"""Knowledge base — log fixes, search fixes, find relevant."""
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

KB_DIR = "kb"
KB_FIXES_LOG = os.path.join(KB_DIR, "fixes.log")
PLAYBOOKS_PATH = os.path.join("playbooks", "PLAYBOOKS.md")

def ensure_kb_dir():
    os.makedirs(KB_DIR, exist_ok=True)

def append_fix(entry_text: str) -> dict:
    """Append a verified fix entry to kb/fixes.log."""
    cleaned = entry_text.strip() if entry_text else ""
    # Strip AI thinking/reasoning blocks that some models emit (e.g. Gemma <thought> tags)
    cleaned = re.sub(r'<thought>.*?</thought>', '', cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', cleaned, flags=re.DOTALL).strip()
    if not cleaned or len(cleaned) < 10:
        return {"success": False, "error": "Entry is empty or too short. Provide a full fix entry with symptom, root cause, fix, and code."}
    ensure_kb_dir()
    try:
        # Ensure entry starts with --- delimiter for consistent search_fixes splitting
        if not cleaned.startswith("---"):
            cleaned = "---\n" + cleaned
        with open(KB_FIXES_LOG, "a", encoding="utf-8") as f:
            f.write(cleaned + "\n")
        return {"success": True, "message": "Fix logged to kb/fixes.log"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def search_fixes(query: str) -> dict:
    """Search kb/fixes.log for entries matching query."""
    if not os.path.exists(KB_FIXES_LOG):
        return {"error": "KB log not found", "matches": []}
    
    matches = []
    try:
        with open(KB_FIXES_LOG, "r", encoding="utf-8") as f:
            content = f.read()
            # Split by the '---' delimiter used in log_fix
            entries = content.split("---")
            for entry in entries:
                if query.lower() in entry.lower():
                    matches.append(entry.strip())
    except Exception as e:
        return {"error": str(e), "matches": []}
    
    return {"query": query, "matches": matches}

def search_playbook(query: str) -> dict:
    """Search PLAYBOOKS.md for fix recipes."""
    if not os.path.exists(PLAYBOOKS_PATH):
        return {"error": "Playbooks not found", "matches": []}
    
    matches = []
    try:
        with open(PLAYBOOKS_PATH, "r", encoding="utf-8") as f:
            content = f.read()
            # Simple section-based search (headers starting with #)
            sections = re.split(r'\n(?=# )', content)
            for section in sections:
                if query.lower() in section.lower():
                    matches.append(section.strip())
    except Exception as e:
        return {"error": str(e), "matches": []}
    
    return {"query": query, "matches": matches[:3]}

CONTEXT_DIR = "context"

def search_context(query: str) -> dict:
    """Search context/*.md files for entries matching query keywords."""
    if not os.path.isdir(CONTEXT_DIR):
        return {"error": "No context/ directory found", "matches": []}

    query_lower = query.lower()
    keywords = [k.strip() for k in re.split(r'[|,\s]+', query_lower) if k.strip()]
    if not keywords:
        return {"error": "Empty query", "matches": []}

    matches = []
    for fname in sorted(os.listdir(CONTEXT_DIR)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(CONTEXT_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        # Split into sections by ## headers
        sections = re.split(r'\n(?=##\s)', content)
        for section in sections:
            section_lower = section.lower()
            if any(kw in section_lower for kw in keywords):
                # Truncate long sections
                preview = section.strip()[:500]
                if len(section.strip()) > 500:
                    preview += "..."
                matches.append({"file": fname, "section": preview})

    if not matches:
        return {"matches": [], "message": f"No context matches for: {query}"}
    return {"matches": matches[:10]}


def find_relevant_fixes(scenario: str, url: str, diagnosis_hints: list = None) -> list:
    """Auto-search KB for entries matching scenario/URL/hints."""
    relevant = []
    
    # Search by scenario
    if scenario and scenario != "generic_web_page":
        res = search_fixes(scenario)
        relevant.extend(res.get("matches", []))
    
    # Search by URL domain
    domain = urlparse(url).netloc
    if domain:
        res = search_fixes(domain)
        relevant.extend(res.get("matches", []))
        
    # Search by hints
    if diagnosis_hints:
        for hint in diagnosis_hints:
            res = search_fixes(hint)
            relevant.extend(res.get("matches", []))
            
    return list(set(relevant))
