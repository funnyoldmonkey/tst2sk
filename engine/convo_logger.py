"""Session conversation logger — saves full session transcripts to convo/ with auto-tags.

Architecture:
  convo/
    tag_index.json          ← lightweight lookup: tag → [filenames]
    session_20260514_143022.json  ← full session transcript + fixes
    session_20260520_091500.json
    ...

Search flow:
  1. AI calls search_conversations("shopify")
  2. We open ONLY tag_index.json (tiny file), find matching filenames
  3. We open ONLY those matched session files, return summaries to AI
  4. If AI needs full conversation, it can request a specific file
"""
import os
import re
import json
import time
import platform
import subprocess
from datetime import datetime
from pathlib import Path

CONVO_DIR = "convo"
TAG_INDEX_PATH = os.path.join(CONVO_DIR, "tag_index.json")


# ─── Clipboard ────────────────────────────────────────────────

def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    try:
        system = platform.system()
        if system == "Windows":
            process = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
            process.communicate(text.encode("utf-16le"))
            return process.returncode == 0
        elif system == "Darwin":
            process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            process.communicate(text.encode("utf-8"))
            return process.returncode == 0
        else:
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                try:
                    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    process.communicate(text.encode("utf-8"))
                    if process.returncode == 0:
                        return True
                except FileNotFoundError:
                    continue
        return False
    except Exception:
        return False


def copy_fix_to_clipboard(fix_code: str, fix_type: str = "fix") -> str:
    """Copy a fix to clipboard and return a status message."""
    success = _copy_to_clipboard(fix_code)
    if success:
        return f"📋 {fix_type} copied to clipboard!"
    else:
        return f"⚠️ Could not copy to clipboard (clipboard tool not available)"


# ─── Tag Index ────────────────────────────────────────────────

def _load_tag_index() -> dict:
    """Load the tag index from disk. Returns {tag: [filename, ...], ...}."""
    if not os.path.exists(TAG_INDEX_PATH):
        return {}
    try:
        with open(TAG_INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return {}


def _save_tag_index(index: dict):
    """Write the tag index to disk."""
    os.makedirs(CONVO_DIR, exist_ok=True)
    with open(TAG_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def _update_tag_index(filename: str, tags: list[str]):
    """Add a session's tags to the index. Called once when a session is saved."""
    index = _load_tag_index()
    for tag in tags:
        if tag not in index:
            index[tag] = []
        if filename not in index[tag]:
            index[tag].append(filename)
    _save_tag_index(index)


def rebuild_tag_index() -> dict:
    """Rebuild the entire tag index from scratch by scanning all session files.
    Useful if the index gets corrupted or out of sync."""
    if not os.path.exists(CONVO_DIR):
        return {}

    index = {}
    for filename in os.listdir(CONVO_DIR):
        if not filename.startswith("session_") or not filename.endswith(".json"):
            continue
        filepath = os.path.join(CONVO_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                doc = json.load(f)
            tags = doc.get("meta", {}).get("tags", [])
            for tag in tags:
                if tag not in index:
                    index[tag] = []
                index[tag].append(filename)
        except Exception:
            continue

    _save_tag_index(index)
    return index


# ─── Tag Generation ──────────────────────────────────────────

def _auto_generate_tags(session_data: dict) -> list[str]:
    """Auto-generate up to 10 searchable tags from session content."""
    tags = set()

    # Tag from URL
    url = session_data.get("url", "")
    if url:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        if domain:
            tags.add(f"site:{domain}")
        path_parts = [p for p in parsed.path.split("/") if p]
        for part in path_parts[:2]:
            if len(part) > 2 and not part.isdigit():
                tags.add(part.lower())

    # Tag from query
    query = session_data.get("query", "").lower()
    query_keywords = {
        "cart": "cart", "checkout": "checkout", "payment": "payment",
        "login": "auth", "sign": "auth", "password": "auth",
        "price": "pricing", "add to cart": "add-to-cart",
        "button": "button", "click": "click-issue",
        "hidden": "visibility", "display": "visibility", "opacity": "visibility",
        "error": "error", "broken": "broken", "missing": "missing",
        "css": "css-issue", "style": "css-issue",
        "script": "js-issue", "javascript": "js-issue",
        "image": "image", "font": "font", "layout": "layout",
        "mobile": "mobile", "responsive": "responsive",
        "shopify": "shopify", "wordpress": "wordpress", "woocommerce": "woocommerce",
        "api": "api", "fetch": "api", "network": "network",
        "form": "form", "input": "form", "submit": "form",
    }
    for keyword, tag in query_keywords.items():
        if keyword in query:
            tags.add(tag)

    # Tag from detected scenario
    scenario = session_data.get("scenario", "")
    if scenario and scenario != "generic_web_page":
        tags.add(scenario)

    # Tag from fix types used
    for fix in session_data.get("fixes", []):
        action = fix.get("action", "")
        if action == "inject_css":
            tags.add("fix:css")
        elif action == "inject_js":
            tags.add("fix:js")

    # Tag from diagnosis hints
    for hint in session_data.get("diagnosis_hints", []):
        hint_lower = hint.lower()
        if "hidden" in hint_lower:
            tags.add("visibility")
        if "console" in hint_lower:
            tags.add("console-errors")
        if "network" in hint_lower or "failed" in hint_lower:
            tags.add("network-errors")

    # Tag the outcome
    if session_data.get("resolved", False):
        tags.add("resolved")
    else:
        tags.add("unresolved")

    return sorted(list(tags))[:10]


# ─── Conversation Logger ─────────────────────────────────────

class ConvoLogger:
    """Logs the full session conversation and saves to convo/ on completion."""

    def __init__(self):
        self.entries: list[dict] = []
        self.session_start = datetime.now()
        self.url = ""
        self.query = ""
        self.scenario = ""
        self.fixes: list[dict] = []
        self.diagnosis_hints: list[str] = []
        self.resolved = False
        self._saved = False

    def log(self, msg_type: str, content: any):
        """Add an entry to the conversation log."""
        if msg_type == "screenshot":
            self.entries.append({
                "time": time.time(),
                "type": msg_type,
                "content": f"[screenshot captured — {content.get('url', 'unknown') if isinstance(content, dict) else 'unknown'}]",
            })
            return

        self.entries.append({
            "time": time.time(),
            "type": msg_type,
            "content": str(content) if not isinstance(content, str) else content,
        })

    def set_session_info(self, url: str, query: str):
        self.url = url
        self.query = query

    def record_fix(self, fix_data: dict):
        self.fixes.append(fix_data)

    def set_scenario(self, scenario: str):
        self.scenario = scenario

    def set_diagnosis_hints(self, hints: list[str]):
        self.diagnosis_hints = hints

    def mark_resolved(self):
        self.resolved = True

    def save(self) -> str:
        """Save the conversation to convo/ and update the tag index."""
        if self._saved:
            return ""

        os.makedirs(CONVO_DIR, exist_ok=True)

        session_data = {
            "url": self.url,
            "query": self.query,
            "scenario": self.scenario,
            "fixes": self.fixes,
            "diagnosis_hints": self.diagnosis_hints,
            "resolved": self.resolved,
        }

        tags = _auto_generate_tags(session_data)
        timestamp = self.session_start.strftime("%Y%m%d_%H%M%S")

        doc = {
            "meta": {
                "timestamp": timestamp,
                "date": self.session_start.strftime("%Y-%m-%d %H:%M:%S"),
                "url": self.url,
                "query": self.query,
                "scenario": self.scenario,
                "resolved": self.resolved,
                "total_turns": len([e for e in self.entries if e["type"] == "status" and "Turn" in e["content"]]),
                "tags": tags,
            },
            "fixes": self.fixes,
            "conversation": [],
        }

        for entry in self.entries:
            doc["conversation"].append({
                "type": entry["type"],
                "content": entry["content"][:2000],
            })

        filename = f"session_{timestamp}.json"
        filepath = os.path.join(CONVO_DIR, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)

            # Update the tag index (only writes to tag_index.json, doesn't touch other files)
            _update_tag_index(filename, tags)

            self._saved = True
            return filepath
        except Exception as e:
            return f"[save error: {e}]"


# ─── Search (uses tag index) ─────────────────────────────────

def search_conversations(query: str) -> list[dict]:
    """Search past sessions using the tag index. Fast — only opens matched files.

    Flow:
      1. Read tag_index.json (one small file)
      2. Find tags that match the query
      3. Collect the filenames from those tags
      4. Open ONLY those files, return summaries
    """
    if not os.path.exists(CONVO_DIR):
        return []

    query_lower = query.lower()
    index = _load_tag_index()

    # Step 1: Find matching filenames from the index
    matched_files = set()
    for tag, filenames in index.items():
        if query_lower in tag.lower():
            matched_files.update(filenames)

    # Step 2: If no tag matches, fall back to a quick meta-only scan
    # (still faster than reading full convos — we only read meta)
    if not matched_files:
        for filename in os.listdir(CONVO_DIR):
            if not filename.startswith("session_") or not filename.endswith(".json"):
                continue
            filepath = os.path.join(CONVO_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                meta = doc.get("meta", {})
                # Check URL, query, scenario only (lightweight)
                if (query_lower in meta.get("url", "").lower()
                    or query_lower in meta.get("query", "").lower()
                    or query_lower in meta.get("scenario", "").lower()):
                    matched_files.add(filename)
            except Exception:
                continue

    # Step 3: Open only matched files and build results
    results = []
    for filename in sorted(matched_files, reverse=True):
        filepath = os.path.join(CONVO_DIR, filename)
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                doc = json.load(f)
            meta = doc.get("meta", {})
            results.append({
                "file": filename,
                "date": meta.get("date", ""),
                "url": meta.get("url", ""),
                "query": meta.get("query", ""),
                "scenario": meta.get("scenario", ""),
                "resolved": meta.get("resolved", False),
                "tags": meta.get("tags", []),
                "fix_count": len(doc.get("fixes", [])),
            })
        except Exception:
            continue

    return results[:10]


def get_conversation_detail(filename: str) -> dict | None:
    """Load a specific session file for the AI to read the full conversation.
    Called when the AI finds a match via search and wants the details."""
    filepath = os.path.join(CONVO_DIR, filename)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
