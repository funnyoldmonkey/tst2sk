"""Subagent infrastructure — specialized AI workers for targeted analysis.

DESIGN: The main agent stays lean (~980 tokens prompt). When it needs deep analysis,
it fires a subagent that reads scratch/ files directly, runs a focused prompt, and
returns clean structured results — no internal thoughts exposed to the main agent.

Each subagent:
- Uses the same model + API keys as the main agent
- Has a 3-second buffer before each API call (rate limit protection)
- Strips <thought>/<thinking> tags from results
- Returns structured data, not raw text
- Has its own timeout (default 120s)
- Rotates API key on 429 errors

Based on SummarizerClient pattern from ai/summarizer.py.
"""
import os
import re
import json
import time
import asyncio
import logging
from openai import AsyncOpenAI
from rich.console import Console

from config import AppConfig

_console = Console()
logger = logging.getLogger("tst2sk.subagent")

SCRATCH_DIR = "scratch"
SUBAGENT_TIMEOUT = 120  # seconds
API_CALL_BUFFER = 3     # seconds — wait before every subagent API call


def _read_scratch(filename: str, max_chars: int = 0) -> str:
    """Read a scratch file, optionally truncated."""
    path = os.path.join(SCRATCH_DIR, filename)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if max_chars and len(content) > max_chars:
            return content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return content
    except Exception:
        return ""


def _read_file(filepath: str, max_chars: int = 0) -> str:
    """Read any file, optionally truncated."""
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if max_chars and len(content) > max_chars:
            return content[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return content
    except Exception:
        return ""


def _strip_thoughts(text: str) -> str:
    """Remove <thought>/<thinking> blocks and other internal reasoning from AI output."""
    text = re.sub(r'<thought>.*?</thought>', '', text, flags=re.DOTALL)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Also strip markdown-style "thinking" headers
    text = re.sub(r'^#{1,3}\s*(?:Thinking|Internal|Reasoning).*?(?=^#{1,3}\s|\Z)', '', text, flags=re.MULTILINE | re.DOTALL)
    return text.strip()


class SubagentClient:
    """Base class for all subagents — handles API calls, retries, key rotation."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.model = config.model

        # Mirror the same key list from the main client
        self._api_keys = config.api_keys if config.api_keys else [config.api_key]
        self._current_key_index = 0

        self.client = self._build_client(self._api_keys[self._current_key_index])

        # Metrics for structured logging
        self._last_call_metrics: dict = {}

    def _build_client(self, api_key: str) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            base_url=self.config.base_url,
            default_headers=self.config.extra_headers if self.config.extra_headers else None,
        )

    def _rotate_key(self, reason: str = "error"):
        """Rotate to the next API key on failure."""
        if len(self._api_keys) <= 1:
            return
        old_idx = self._current_key_index
        self._current_key_index = (self._current_key_index + 1) % len(self._api_keys)
        self.client = self._build_client(self._api_keys[self._current_key_index])
        _console.print(f"  [dim]🔄 Subagent key rotated ({reason}): {old_idx + 1} → {self._current_key_index + 1}/{len(self._api_keys)}[/dim]")

    async def _call_ai(
        self,
        system_prompt: str,
        user_content: str,
        subagent_name: str = "subagent",
        temperature: float = 0.1,
        max_tokens: int = 2048,
        timeout: int = SUBAGENT_TIMEOUT,
    ) -> str | None:
        """Make a single AI call with buffer, timeout, retry on 429.

        Returns cleaned text (thoughts stripped) or None on failure.
        """
        # Rate-limit buffer before API call
        _console.print(f"  [dim]⏳ {subagent_name}: {API_CALL_BUFFER}s buffer before API call...[/dim]")
        await asyncio.sleep(API_CALL_BUFFER)

        _console.print(f"  [bold magenta]🧠 {subagent_name}: calling AI...[/bold magenta]")

        attempts = 0
        max_attempts = 3
        last_error = None

        while attempts < max_attempts:
            attempts += 1
            try:
                t0 = time.monotonic()
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        temperature=temperature,
                        max_tokens=max_tokens,
                    ),
                    timeout=timeout,
                )
                elapsed = round(time.monotonic() - t0, 1)

                # Extract usage metrics
                usage = getattr(response, "usage", None)
                self._last_call_metrics = {
                    "subagent": subagent_name,
                    "elapsed_s": elapsed,
                    "model": self.model,
                    "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                    "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                    "attempts": attempts,
                }

                text = (response.choices[0].message.content or "").strip()
                if not text or len(text) < 10:
                    _console.print(f"  [dim red]⚠️ {subagent_name}: empty/short response[/dim red]")
                    return None

                # Log metrics
                tokens_str = f" | tokens={self._last_call_metrics.get('total_tokens', '?')}" if usage else ""
                _console.print(f"  [dim]⚡ {subagent_name}: {elapsed}s{tokens_str}[/dim]")

                # Strip internal thoughts
                cleaned = _strip_thoughts(text)
                _console.print(f"  [dim]📤 {subagent_name} output: {len(cleaned):,} chars (raw: {len(text):,})[/dim]")
                return cleaned

            except asyncio.TimeoutError:
                _console.print(f"  [dim red]⚠️ {subagent_name}: timed out after {timeout}s[/dim red]")
                self._rotate_key("timeout")
                last_error = "timeout"
                # Wait before retry
                await asyncio.sleep(API_CALL_BUFFER)

            except Exception as e:
                last_error = e
                error_str = str(e)
                is_rate_limit = "429" in error_str or "rate" in error_str.lower() or "quota" in error_str.lower()

                if is_rate_limit and attempts < max_attempts:
                    self._rotate_key("429 rate limit")
                    _console.print(f"  [yellow]⚠️ {subagent_name}: rate limited, retrying after buffer...[/yellow]")
                    await asyncio.sleep(API_CALL_BUFFER * 2)  # Double buffer on rate limit
                    continue

                _console.print(f"  [dim red]⚠️ {subagent_name} failed ({type(e).__name__}): {error_str[:150]}[/dim red]")
                if is_rate_limit:
                    self._rotate_key("429 rate limit")
                return None

        _console.print(f"  [dim red]⚠️ {subagent_name}: all {max_attempts} attempts failed (last: {last_error})[/dim red]")
        return None

    def get_last_metrics(self) -> dict:
        """Return metrics from the last API call for structured logging."""
        return self._last_call_metrics.copy()


# ═══════════════════════════════════════════════════════════════════════════════
# INVESTIGATE SUBAGENT
# Replaces: diagnose + search_context + search_playbook + search_fixes
# Reads all scratch files + context/ + playbooks/ + kb/
# Returns structured briefing for the main agent
# ═══════════════════════════════════════════════════════════════════════════════

INVESTIGATE_SYSTEM = """You are an investigation analyst for a web debugging agent. Your job is to analyze page state data and produce a structured briefing.

You receive: DOM snapshot, console logs, network logs, product context docs, playbook recipes, and past fix entries.

Produce a briefing in EXACTLY this format (no other format, no markdown outside):

```json
{
  "scenario": "shopify_product | shopify_store | shopify_checkout | generic_web_page",
  "confidence": 85,
  "platform_details": {
    "theme": "Dawn | Debut | unknown",
    "page_type": "product | collection | cart | checkout | unknown",
    "apps_detected": ["Back In Stock", "Slide Cart"]
  },
  "critical_info": [
    "BIS modal renders inside #BIS_frame iframe with src=about:blank",
    "inject_css CANNOT reach inside iframes — must use inject_js targeting frame.contentDocument"
  ],
  "console_errors": {
    "real": ["TypeError: Cannot read property 'x' of null"],
    "suspicious": ["Error referencing unknown module — verify with DOM"],
    "noise_count": 5
  },
  "hidden_elements": ["#BIS_modal [HIDDEN: display:none]"],
  "relevant_playbook_recipes": [
    {"title": "BIS Modal Styling", "approach": "Use inject_js to access iframe contentDocument, apply styles there"}
  ],
  "past_fixes": [
    {"store": "example.com", "issue": "BIS modal font", "fix_type": "inject_js", "snippet": "..."}
  ],
  "recommended_approach": "1. Open BIS modal by clicking Notify Me. 2. Access iframe via inject_js. 3. Style contentDocument. 4. Verify with run_test inside iframe.",
  "warnings": ["inject_css will NOT work for this issue — iframe boundary blocks it"]
}
```

Rules:
1. Be CONCISE — the main agent has limited context window
2. Only include fields that have actual data — omit empty arrays/objects
3. critical_info should contain the TOP 3-5 things the main agent MUST know
4. recommended_approach should be a numbered action plan, not vague advice
5. warnings should flag common mistakes for this scenario
6. Do NOT include your reasoning process — only the structured result"""


class InvestigateSubagent(SubagentClient):
    """Deep investigation — reads all available data and produces a structured briefing."""

    async def run(self, query: str, url: str) -> dict | None:
        """Run investigation and return structured briefing dict, or None on failure."""
        from engine.diagnostics import cross_reference_diagnostics

        # Step 1: Run deterministic diagnostics (instant, no AI)
        diag = cross_reference_diagnostics()

        # Step 2: Gather all data sources
        dom_preview = _read_scratch("obs_dom.txt", max_chars=4000)
        console_log = _read_scratch("obs_console.log", max_chars=2000)
        network_log = _read_scratch("obs_network.log", max_chars=2000)

        # Read context files
        context_content = ""
        context_dir = "context"
        if os.path.isdir(context_dir):
            for fname in sorted(os.listdir(context_dir)):
                if fname.endswith(".md"):
                    content = _read_file(os.path.join(context_dir, fname), max_chars=4000)
                    if content:
                        context_content += f"\n--- {fname} ---\n{content}\n"

        # Read playbooks
        playbook_content = _read_file("playbooks/PLAYBOOKS.md", max_chars=3000)

        # Read past fixes
        fixes_content = _read_file("kb/fixes.log", max_chars=3000)

        # Step 3: Build user content for subagent
        user_content = f"""URL: {url}
User Query: {query}

=== DIAGNOSTIC RESULTS (deterministic) ===
Scenario: {diag.get('detected_scenario', 'unknown')} (confidence: {diag.get('confidence', 0)}%)
Potential Issues: {json.dumps(diag.get('potential_issues', []))}
Console Error Classification: {json.dumps(diag.get('console_error_classification', {}))}
Shopify Analysis: {json.dumps(diag.get('shopify_analysis', {}), indent=1) if diag.get('shopify_analysis') else 'N/A'}
Scripts: {json.dumps(diag.get('scripts', [])[:15])}

=== DOM SNAPSHOT (first 4000 chars) ===
{dom_preview}

=== CONSOLE LOG ===
{console_log}

=== NETWORK LOG ===
{network_log}

=== PRODUCT CONTEXT DOCS ===
{context_content if context_content else 'No context files found.'}

=== PLAYBOOK RECIPES ===
{playbook_content if playbook_content else 'No playbooks found.'}

=== PAST FIX ENTRIES (kb/fixes.log) ===
{fixes_content if fixes_content else 'No past fixes.'}"""

        # Log input sizes for debugging
        input_size = len(user_content)
        _console.print(f"  [dim]🕵️ investigate input: {input_size:,} chars "
                       f"(DOM={len(dom_preview)}, console={len(console_log)}, network={len(network_log)}, "
                       f"context={len(context_content)}, playbook={len(playbook_content)}, fixes={len(fixes_content)})[/dim]")

        # Step 4: Call AI
        raw = await self._call_ai(
            system_prompt=INVESTIGATE_SYSTEM,
            user_content=user_content,
            subagent_name="investigate",
            max_tokens=2048,
        )

        if not raw:
            # Fallback: return deterministic diagnostics
            _console.print(f"  [yellow]⚠️ Investigate subagent failed — falling back to deterministic diagnostics[/yellow]")
            return {
                "scenario": diag.get("detected_scenario", "unknown"),
                "confidence": diag.get("confidence", 0),
                "console_errors": diag.get("console_error_classification", {}),
                "hidden_elements": diag.get("hidden_elements", [])[:5],
                "warnings": ["Investigate subagent failed — using basic diagnostics only"],
                "_fallback": True,
            }

        # Step 5: Parse JSON from response
        try:
            # Extract JSON from markdown fences if present
            json_text = raw
            if "```json" in json_text:
                json_text = json_text.split("```json")[1].split("```")[0].strip()
            elif "```" in json_text:
                json_text = json_text.split("```")[1].split("```")[0].strip()

            start = json_text.find("{")
            end = json_text.rfind("}") + 1
            if start != -1 and end > start:
                json_text = json_text[start:end]

            result = json.loads(json_text)

            # Inject deterministic data the AI might have missed
            if "scenario" not in result:
                result["scenario"] = diag.get("detected_scenario", "unknown")
            if "confidence" not in result:
                result["confidence"] = diag.get("confidence", 0)

            # Attach context file names for JIT tracking
            if os.path.isdir(context_dir):
                result["_context_files_read"] = [f for f in sorted(os.listdir(context_dir)) if f.endswith(".md")]

            return result

        except (json.JSONDecodeError, ValueError) as e:
            _console.print(f"  [yellow]⚠️ Investigate: JSON parse failed ({e}) — returning raw text as briefing[/yellow]")
            return {
                "scenario": diag.get("detected_scenario", "unknown"),
                "confidence": diag.get("confidence", 0),
                "briefing_text": raw[:3000],
                "warnings": ["Subagent returned non-JSON — raw text included as briefing_text"],
                "_parse_failed": True,
            }


# ═══════════════════════════════════════════════════════════════════════════════
# VERIFY FIX SUBAGENT — Multi-Turn QA Agent
# Runs its own observe→think→act loop with full browser access.
# Clicks elements, checks computed styles via CDP, injects test code,
# looks for regressions, and returns a structured verdict.
# The main agent CANNOT report until this subagent passes.
# ═══════════════════════════════════════════════════════════════════════════════

VERIFY_FIX_SYSTEM = """# QA Verification Agent

You are a strict, thorough QA tester. You interact with the browser like a REAL USER to verify fixes work. You click buttons, scroll pages, type in inputs, and hover elements — just like a customer would. Then you use CDP to confirm exact CSS values.

## Context
You receive: the original issue, what fixes were applied, and a live browser observation.

## Your Mission — 3 Phases (ALL required)

### Phase 1: Interact (at least 2 turns)
Test every fix the way a real user would:
- **Click** buttons/links that were fixed — verify they respond
- **Type** into inputs that were fixed — verify they accept input
- **Scroll** to fixed elements — verify they're in the right position
- **Hover** over elements — verify hover states work
If a click times out or fails, fall back to `run_test` to check the element's state programmatically (disabled, pointer-events, dimensions). Note the limitation in your verdict.

### Phase 2: Inspect (at least 1 turn)
Get hard data on every fix:
- **`cdp_get_computed_style`** on each fixed selector — verify exact CSS values match intent
- **`cdp_query_selector_all`** to check element visibility, dimensions, disabled state
- **`run_test`** for complex checks (iframe content, computed values, element existence)

### Phase 3: Regression Check (at least 1 turn)
- **Scroll** the full page — look for layout shifts, broken elements, missing content
- **Click** nearby interactive elements — verify they still work
- Check console for new errors

## Available Actions
Respond with ONE JSON action per turn. You get fresh observation data after each action.

**Real user interactions (USE THESE FIRST):**
- `click` { "selector": "..." } — Click an element to test it
- `scroll` { "x": 0, "y": 500 } — Scroll to check layout and find regressions
- `type` { "selector": "...", "text": "..." } — Type into inputs to test them
- `hover` { "selector": "..." } — Test hover states
- `set_viewport_size` { "width": 375, "height": 667 } — Resize viewport (e.g., mobile: 375x667, desktop: 1440x900) to test media queries natively.

**Inspection tools:**
- `observe` {} — Get fresh page state
- `inspect_element` { "selector": "..." } — Get computed styles, rect, attributes
- `run_test` { "code": "..." } — Run JS that returns data. Use `return` to get results.
- `cdp_query_selector_all` { "selector": "..." } — Batch lookup: visibility, rect, disabled state
- `cdp_get_computed_style` { "selector": "..." } — Full computed style from Chrome DevTools
- `cdp_get_matched_styles` { "selector": "..." } — Full CSS cascade rules matching the selector (inline and rule matches, including media queries and origins)
- `cdp_get_event_listeners` { "selector": "..." } — All JS event listeners registered directly on the matching element (handler descriptions, script location/line/col)
- `search_all_frames` { "selector": "..." } — Deep search matching selector in all nested cross-origin/same-origin frames and Shadow DOM roots
- `cdp_get_network_details` { "urlPattern": "..." } — Full HTTP details (request method, headers, POST body, response headers, response payload) for matching captured network requests

**Fix recovery & Clean Room:**
- `reapply_fixes` {} — Re-injects ALL fixes from the main agent. Use ONLY if a page refresh/reload wiped the fixes. This replays the exact same code — it does NOT create new fixes.
- `reload` {} — Reloads the page to clear all temporary probes and start fresh.

**Probing (for testing hypotheses):**
- `inject_css` { "css": "..." } — Temporarily inject CSS into the page to test a potential fix.
- `inject_js` { "code": "..." } — Temporarily inject JS into the page to test a potential fix.

**End the loop:**
- `verdict` { "passed": true/false, "confidence": 0-100, "checks": [...], "regressions": [...], "recommendation": "..." }

## Output Format — STRICT
```
{
  "thought": "I see: [observation]. Testing: [what and why].",
  "action": "action_name",
  "payload": { ... }
}
```

## Rules
- **Interact BEFORE inspecting.** You MUST click/scroll/type/hover BEFORE using CDP or run_test. Real user testing comes first.
- **Be thorough.** Check EVERY fix, not just the last one. Each fix needs at least one interaction AND one CDP/inspection check.
- **NEVER fix anything permanently.** You are a verifier. You may use `inject_css` or `inject_js` ONLY to probe/test a hypothesis.
- **Probing Rules:**
  - You CANNOT return `verdict` with `passed: true` if any probe has been injected unless you first reload/refresh the page (via `reload`) to clear the probes, re-apply the main agent's official fixes via `reapply_fixes`, and verify they work in a clean room environment without any active probes.
  - If a probe works but the official fixes do not, return `verdict` with `passed: false` and describe the working fix in your `recommendation` so the main agent can officially apply it.
- **Click timeout = fallback, not skip.** If click fails, use `run_test` to check element state. Report "click timed out, verified via DOM" and lower confidence.
- **Page refreshed?** If fixes disappear (elements revert to broken state), use `reapply_fixes` to re-inject them, then continue testing.
- **Scroll for regressions.** You MUST scroll the page at least once to check for broken layout or missing elements beyond the viewport.
- **Be STRICT.** Only PASSED if you have concrete evidence from BOTH interaction AND inspection for every fix. If anything is ambiguous, FAIL it.
- **Max turns:** Budget is dynamic based on number of fixes. Pay attention to how many turns you have remaining. Don't waste turns.

## Verdict Format
PASSED — only when ALL fixes are confirmed working via interaction + inspection:
```json
{
  "passed": true,
  "confidence": 95,
  "checks": [
    {"what": "BIS modal email input border", "method": "cdp_get_computed_style", "expected": "1px solid #999", "actual": "1px solid rgb(153, 153, 153)", "passed": true},
    {"what": "BIS email input accepts text", "method": "type", "expected": "accepts input", "actual": "typed successfully", "passed": true},
    {"what": "Add to cart button clickable", "method": "click", "expected": "responds to click", "actual": "clicked OK", "passed": true}
  ],
  "regressions": [],
  "recommendation": "All fixes verified via interaction and inspection. Safe to report."
}
```
FAILED — when ANY fix doesn't work or regressions found:
```json
{
  "passed": false,
  "confidence": 85,
  "checks": [
    {"what": "Bundle widget spacing", "method": "cdp_get_computed_style", "expected": "margin-top: 16px", "actual": "margin-top: 0px", "passed": false},
    {"what": "Sold out button hidden", "method": "click", "expected": "not found/hidden", "actual": "still visible and clickable", "passed": false}
  ],
  "regressions": ["Cart button no longer clickable after fix"],
  "recommendation": "Bundle widget spacing fix did not apply (margin-top is 0px). Sold out button is still visible. Cart button broke — regression."
}
```
"""

# Allowed actions for the QA agent (subset of main agent actions)
_VERIFY_ALLOWED_ACTIONS = {
    "click", "scroll", "type", "hover", "observe",
    "inspect_element", "run_test",
    "cdp_query_selector_all", "cdp_get_computed_style",
    "cdp_get_matched_styles", "cdp_get_event_listeners",
    "search_all_frames", "cdp_get_network_details",
    "reapply_fixes", "verdict",
    "inject_css", "inject_js", "reload", "set_viewport_size",
}


def _parse_json_response(raw: str) -> dict | None:
    """Extract a JSON object from an AI response. Returns parsed dict or None."""
    # Try direct parse first
    text = raw.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown fences
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # Find outermost { }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
    return None


class VerifyFixSubagent(SubagentClient):
    """Multi-turn QA agent that interacts with the browser to verify ALL fixes.

    Runs its own observe→think→act loop (max 8 turns). Acts like a real QA tester:
    Phase 1 — Interact (click, scroll, type, hover the fixed elements)
    Phase 2 — Inspect (CDP computed styles, run_test assertions)
    Phase 3 — Regression check (scroll page, click nearby elements)
    Verdict blocked until at least 1 interaction is done.
    Can reapply_fixes if page refresh wipes injected code.
    Returns structured verdict: passed/failed with checks and regressions.
    """

    MAX_TURNS = 15  # Default cap; brain.py passes dynamic qa_budget (8-15) as max_turns

    async def run(
        self,
        query: str,
        scenario: str,
        fix_attempts: list[dict],
        browser,           # BrowserController instance
        capture_fn,        # capture_observation(browser) -> dict
        execute_fn,        # execute_action(browser, action, payload) -> str
        multimodal: bool = True,
        max_turns: int = 8,
    ) -> dict | None:
        """Run multi-turn QA verification loop.

        Returns structured verdict dict:
        {
            "passed": bool,
            "confidence": int,
            "checks": [{"what": str, "expected": str, "actual": str, "passed": bool}],
            "regressions": [str],
            "recommendation": str,
        }
        """
        # Build fix context for the QA agent — include selectors for direct verification
        fixes_summary = []
        selectors_used = []
        for fix in fix_attempts:
            payload = fix.get("payload", {})
            code = payload.get("code") or payload.get("js") or payload.get("css") or ""
            fixes_summary.append({
                "turn": fix.get("turn"),
                "action": fix.get("action"),
                "code": code,
                "thought": fix.get("thought") or "",
            })
            # Extract CSS selectors from fix code for the QA agent
            # CSS: match selectors before { blocks
            if fix.get("action") == "inject_css" and code:
                import re as _re
                css_selectors = _re.findall(r'([.#][\w\-]+(?:\s+[.#>~+\w\-\[\]="\'*:]+)*)\s*\{', code)
                selectors_used.extend(css_selectors)
            # JS: extract querySelector/querySelectorAll arguments
            if fix.get("action") == "inject_js" and code:
                import re as _re
                js_selectors = _re.findall(r"querySelector(?:All)?\(['\"]([^'\"]+)['\"]\)", code)
                selectors_used.extend(js_selectors)
        # Deduplicate selectors
        selectors_used = list(dict.fromkeys(selectors_used))

        # Capture initial observation
        try:
            obs = await capture_fn(browser)
        except Exception as e:
            _console.print(f"  [red]❌ verify_fix: failed to capture initial observation: {e}[/red]")
            return {"passed": False, "confidence": 0, "checks": [], "regressions": [],
                    "recommendation": f"QA agent could not capture page state: {e}"}

        # Build initial user message with fix context + observation
        obs_dom = obs.get("dom", "")[:3000]
        obs_console = obs.get("console", "")[:1000]
        obs_url = obs.get("url", "")

        selectors_block = ""
        if selectors_used:
            selectors_block = f"""
=== SELECTORS USED IN FIXES (use these — do NOT guess new ones) ===
{chr(10).join(f'  - {s}' for s in selectors_used)}
"""

        initial_message = f"""ORIGINAL ISSUE: {query}
SCENARIO: {scenario}
URL: {obs_url}

=== FIXES TO VERIFY ({len(fixes_summary)}) ===
{json.dumps(fixes_summary, indent=1)}
{selectors_block}
=== CURRENT PAGE STATE ===
DOM (first 3000 chars):
{obs_dom}

Console:
{obs_console}

Your job: Verify ALL {len(fixes_summary)} fix(es) actually work, then check for regressions. Use the EXACT selectors listed above — do NOT invent new ones. Start by inspecting the fixed elements."""

        # Build conversation history
        messages = [
            {"role": "system", "content": VERIFY_FIX_SYSTEM},
        ]

        # Add initial user message (with screenshot if multimodal)
        if multimodal and obs.get("screenshot_base64"):
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": initial_message},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{obs['screenshot_base64']}",
                    }},
                ],
            })
        else:
            messages.append({"role": "user", "content": initial_message})

        _console.print(f"  [bold magenta]🔍 QA Agent: starting verification ({len(fixes_summary)} fixes, max {max_turns} turns)[/bold magenta]")

        # Track interactions — must have at least 1 before verdict
        _interaction_actions = {"click", "scroll", "type", "hover"}
        _inspection_actions = {
            "cdp_get_computed_style", "cdp_query_selector_all", "inspect_element", "run_test",
            "cdp_get_matched_styles", "cdp_get_event_listeners", "search_all_frames", "cdp_get_network_details"
        }
        interactions_done = 0
        inspections_done = 0
        probes_active = False
        _verdict_rejections = 0  # Track how many times verdict was rejected

        # Multi-turn loop
        verdict = None
        for turn in range(1, max_turns + 1):
            _console.print(f"  [dim]🔍 QA turn {turn}/{max_turns}...[/dim]")

            # Rate-limit buffer
            _console.print(f"  [dim]⏳ verify_fix: {API_CALL_BUFFER}s buffer...[/dim]")
            await asyncio.sleep(API_CALL_BUFFER)

            # Call AI
            try:
                t0 = time.monotonic()
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=0.1,
                        max_tokens=1024,
                    ),
                    timeout=SUBAGENT_TIMEOUT,
                )
                elapsed = round(time.monotonic() - t0, 1)

                usage = getattr(response, "usage", None)
                self._last_call_metrics = {
                    "subagent": "verify_fix",
                    "elapsed_s": elapsed,
                    "model": self.model,
                    "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                    "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                    "qa_turn": turn,
                }

                raw_text = (response.choices[0].message.content or "").strip()
                tokens_str = f" | tokens={self._last_call_metrics.get('total_tokens', '?')}" if usage else ""
                _console.print(f"  [dim]⚡ QA turn {turn}: {elapsed}s{tokens_str}[/dim]")

            except asyncio.TimeoutError:
                _console.print(f"  [red]⚠️ QA turn {turn}: timed out[/red]")
                self._rotate_key("timeout")
                break
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "rate" in error_str.lower()
                if is_rate_limit:
                    self._rotate_key("429")
                    await asyncio.sleep(API_CALL_BUFFER * 2)
                    continue
                _console.print(f"  [red]⚠️ QA turn {turn} failed: {error_str[:150]}[/red]")
                break

            if not raw_text or len(raw_text) < 5:
                _console.print(f"  [yellow]⚠️ QA turn {turn}: empty response — nudging[/yellow]")
                # Don't break — nudge the agent to continue
                messages.append({"role": "user", "content": (
                    "SYSTEM: Your response was empty. You MUST take action NOW. "
                    f"You have {max_turns - turn} turns remaining. "
                    "If you've verified enough, submit a verdict. Otherwise, inspect the next fix."
                )})
                continue

            # Strip thinking tags
            cleaned = _strip_thoughts(raw_text)

            # Parse JSON response
            parsed = _parse_json_response(cleaned)
            if not parsed:
                _console.print(f"  [yellow]⚠️ QA turn {turn}: JSON parse failed — nudging[/yellow]")
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({"role": "user", "content": "Your response was not valid JSON. Respond with ONLY a JSON object: {\"thought\": \"...\", \"action\": \"...\", \"payload\": {...}}"})
                continue

            thought = parsed.get("thought", "")
            action = parsed.get("action", "")
            payload = parsed.get("payload", {})

            # Empty thought / empty action detection — same sentinel issue as main loop
            _EMPTY_SENTINELS = {"", "[empty response from model]"}
            if (thought.strip().lower() in _EMPTY_SENTINELS and not action) or action == "observe":
                _console.print(f"  [yellow]⚠️ QA turn {turn}: empty/stalled response — nudging[/yellow]")
                messages.append({"role": "assistant", "content": cleaned})
                remaining = max_turns - turn
                if remaining <= 2:
                    # Running out of turns — force verdict
                    messages.append({"role": "user", "content": (
                        f"SYSTEM: Only {remaining} turn(s) left. You MUST submit a verdict NOW. "
                        "Use action 'verdict' with your best assessment of passed/failed based on what you've seen."
                    )})
                else:
                    messages.append({"role": "user", "content": (
                        "SYSTEM: Your response was empty or stalled. Take a concrete verification action. "
                        f"Use the selectors from the fix list. {remaining} turns remaining."
                    )})
                continue

            _console.print(f"  [cyan]🔍 QA [{turn}] {action}[/cyan] — {thought[:120]}")

            # Track probes
            if action in ("inject_css", "inject_js"):
                probes_active = True
            elif action in ("navigate", "reload"):
                probes_active = False

            # Add assistant message to history
            messages.append({"role": "assistant", "content": cleaned})

            # Track action categories
            if action in _interaction_actions:
                interactions_done += 1
            elif action in _inspection_actions:
                inspections_done += 1

            # Handle verdict — end the loop (with interaction gate)
            if action == "verdict":
                verdict_payload = payload if isinstance(payload, dict) else {}
                # Block early verdict if no interactions were done for a passed verdict
                if interactions_done == 0 and verdict_payload.get("passed", False):
                    _console.print(f"  [yellow]⚠️ QA turn {turn}: verdict rejected — no interactions yet[/yellow]")
                    messages.append({"role": "user", "content": (
                        "⛔ REJECTED: You cannot return a PASSED verdict without interacting with the page first. "
                        "You MUST click, scroll, type, or hover on the fixed elements before judging. "
                        "Start Phase 1: click a fixed element or scroll to check layout."
                    )})
                    continue

                # Block verdict if probes are active and verdict is passed
                if probes_active and verdict_payload.get("passed", False):
                    _verdict_rejections += 1
                    if _verdict_rejections >= 2:
                        # After 2 rejections, auto-handle: reload + reapply + accept next verdict
                        _console.print(f"  [yellow]⚠️ QA turn {turn}: verdict rejected {_verdict_rejections}x — auto-resolving probes[/yellow]")
                        # Auto-reload
                        try:
                            await execute_fn(browser, "reload", {})
                            await asyncio.sleep(2)
                        except Exception:
                            pass
                        # Auto-reapply fixes
                        _console.print(f"  [magenta]🔄 QA: auto-reapplying {len(fix_attempts)} fix(es) after probe cleanup...[/magenta]")
                        for fix in fix_attempts:
                            fix_action = fix.get("action", "")
                            fix_payload = fix.get("payload", {})
                            if fix_action in ("inject_css", "inject_js"):
                                try:
                                    await execute_fn(browser, fix_action, fix_payload)
                                except Exception:
                                    pass
                        probes_active = False
                        # Inject a message telling the agent probes are cleared
                        messages.append({"role": "user", "content": (
                            "SYSTEM: Probes have been auto-cleared. Page was reloaded and official fixes re-applied. "
                            "You may now submit your verdict. probes_active = false."
                        )})
                        continue
                    else:
                        _console.print(f"  [yellow]⚠️ QA turn {turn}: verdict rejected — probes are active[/yellow]")
                        messages.append({"role": "user", "content": (
                            "⛔ REJECTED: You cannot return a PASSED verdict while temporary probe injections (inject_css/inject_js) are active. "
                            "You must either:\n"
                            "1. Reload the page using 'reload', re-apply the official fixes using 'reapply_fixes', and verify they work in a clean state, OR\n"
                            "2. Return a FAILED verdict (passed: false) with your recommended fix in the 'recommendation' field so the main agent can officially apply it."
                        )})
                        continue

                verdict = verdict_payload
                # Ensure required fields
                verdict.setdefault("passed", False)
                verdict.setdefault("confidence", 0)
                verdict.setdefault("checks", [])
                verdict.setdefault("regressions", [])
                verdict.setdefault("recommendation", "")
                verdict["_qa_turns"] = turn
                verdict["_interactions"] = interactions_done
                verdict["_inspections"] = inspections_done
                _console.print(f"  [bold {'green' if verdict['passed'] else 'red'}]"
                              f"{'✅' if verdict['passed'] else '❌'} QA Verdict: "
                              f"{'PASSED' if verdict['passed'] else 'FAILED'} "
                              f"(confidence: {verdict['confidence']}%, {turn} turns, "
                              f"{interactions_done} interactions, {inspections_done} inspections)"
                              f"[/bold {'green' if verdict['passed'] else 'red'}]")
                return verdict

            # Validate action
            if action not in _VERIFY_ALLOWED_ACTIONS:
                _console.print(f"  [yellow]⚠️ QA turn {turn}: disallowed action '{action}' — nudging[/yellow]")
                messages.append({"role": "user", "content": f"Action '{action}' is not available. Use one of: {', '.join(sorted(_VERIFY_ALLOWED_ACTIONS))}"})
                continue

            # Handle reapply_fixes — replay main agent's fix code
            if action == "reapply_fixes":
                _console.print(f"  [magenta]🔄 QA: re-applying {len(fix_attempts)} fix(es)...[/magenta]")
                reapply_results = []
                for fix in fix_attempts:
                    fix_action = fix.get("action", "")
                    fix_payload = fix.get("payload", {})
                    if fix_action in ("inject_css", "inject_js"):
                        try:
                            r = await execute_fn(browser, fix_action, fix_payload)
                            reapply_results.append(f"{fix_action}: OK")
                        except Exception as e:
                            reapply_results.append(f"{fix_action}: FAILED ({e})")
                action_result = f"Re-applied {len(reapply_results)} fixes: {'; '.join(reapply_results)}"
                _console.print(f"  [magenta]🔄 {action_result}[/magenta]")
                # Skip normal execute_fn — we already handled it
                try:
                    obs = await capture_fn(browser)
                except Exception:
                    obs = {}
                obs_dom_snippet = obs.get("dom", "")[:2000]
                obs_console_snippet = obs.get("console", "")[:500]
                obs_text = f"""Action result for reapply_fixes:\n{action_result}\n\n=== UPDATED PAGE STATE ===\nDOM (first 2000 chars):\n{obs_dom_snippet}\n\nConsole:\n{obs_console_snippet}"""
                if multimodal and obs.get("screenshot_base64"):
                    messages.append({"role": "user", "content": [
                        {"type": "text", "text": obs_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{obs['screenshot_base64']}"}},
                    ]})
                else:
                    messages.append({"role": "user", "content": obs_text})
                continue

            # Execute browser action
            try:
                action_result = await execute_fn(browser, action, payload)
            except Exception as e:
                action_result = f"Error executing {action}: {e}"
                _console.print(f"  [yellow]⚠️ QA action error: {str(e)[:100]}[/yellow]")

            # Capture fresh observation after browser action
            try:
                obs = await capture_fn(browser)
            except Exception:
                obs = {}

            # Build observation message for next turn
            obs_dom_snippet = obs.get("dom", "")[:2000]
            obs_console_snippet = obs.get("console", "")[:500]

            # Add turn budget warning when running low
            remaining_turns = max_turns - turn
            budget_warning = ""
            if remaining_turns <= 3:
                budget_warning = f"\n⏰ BUDGET WARNING: Only {remaining_turns} turn(s) remaining. Submit your verdict soon!"
            elif remaining_turns <= 5:
                budget_warning = f"\n⏰ {remaining_turns} turns remaining."

            obs_text = f"""Action result for {action}:
{str(action_result)[:2000]}

=== UPDATED PAGE STATE ===
DOM (first 2000 chars):
{obs_dom_snippet}

Console:
{obs_console_snippet}{budget_warning}"""

            # Attach screenshot if multimodal
            if multimodal and obs.get("screenshot_base64"):
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": obs_text},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{obs['screenshot_base64']}",
                        }},
                    ],
                })
            else:
                messages.append({"role": "user", "content": obs_text})

        # If we exhausted turns without a verdict, force a fail
        if verdict is None:
            _console.print(f"  [bold red]❌ QA Agent: exhausted {max_turns} turns without verdict — auto-failing[/bold red]")
            return {
                "passed": False,
                "confidence": 0,
                "checks": [],
                "regressions": [],
                "recommendation": f"QA agent ran {max_turns} turns without reaching a verdict. Manual verification needed.",
                "_qa_turns": max_turns,
                "_auto_failed": True,
            }

        return verdict


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD REPORT SUBAGENT
# Reads session fix data and verification results
# Returns properly formatted report with all required sections
# ═══════════════════════════════════════════════════════════════════════════════

BUILD_REPORT_SYSTEM = """You are a report builder for a web debugging agent. Your job is to compile a professional support report from session data.

The report MUST contain ALL 4 sections below. Missing any section is a failure.

If the verification succeeded, write a Success Report.
If the verification failed, write a Failure/Escalation Report (explaining what fixes were attempted, why they failed verification, and what is suspected).

Format:

## Root Cause
[Clear 1-3 sentence explanation of what was wrong/suspected and why]

## Fix Applied
```css
/* or ```javascript — the actual fix code or attempted fixes */
.element { property: value; }
```

## Where to Implement
[Exact location: which Shopify admin section, which file, which CSS selector to target]
[For theme CSS: Online Store > Themes > Edit Code > Assets/base.css (or custom.css)]
[For app-specific: which app's settings page]

## Verification
[If succeeded: what was tested and confirmed working]
[If failed: what was tested, how it failed verification, and the reasons/recommendations]

---
Type **Summarize** to save this fix to the knowledge base, then **End** to close the session.

Rules:
1. ALL 4 sections are MANDATORY — never skip any
2. Fix code must be the final version (working version if succeeded, best attempt if failed)
3. "Where to Implement" must be specific enough for a merchant to follow
4. Keep it concise — this goes directly to the user
5. Do NOT add your own commentary or thinking — just the report"""


class BuildReportSubagent(SubagentClient):
    """Builds a properly formatted support report from session data."""

    async def run(self, query: str, url: str, scenario: str, fixes: list[dict],
                  verification: dict | None = None, plan: list[dict] | None = None) -> str | None:
        """Build report and return formatted markdown string, or None on failure."""
        # Format fixes for the subagent
        fix_summary = []
        for fix in fixes:
            fix_summary.append({
                "turn": fix.get("turn"),
                "action": fix.get("action"),
                "code": (fix.get("payload", {}).get("code") or fix.get("payload", {}).get("css") or "")[:500],
                "thought": fix.get("thought", "")[:200],
            })

        # Format plan findings
        plan_findings = ""
        if plan:
            for item in plan:
                findings = item.get("findings", "")
                if findings:
                    plan_findings += f"- {item['task']}: {findings}\n"

        user_content = f"""URL: {url}
User Query: {query}
Scenario: {scenario}

=== FIX ATTEMPTS ({len(fix_summary)} total) ===
{json.dumps(fix_summary, indent=1)}

=== VERIFICATION RESULTS ===
{json.dumps(verification, indent=1) if verification else 'No automated verification available'}

=== PLAN FINDINGS ===
{plan_findings if plan_findings else 'No plan findings available'}"""

        # Log input sizes for debugging
        input_size = len(user_content)
        _console.print(f"  [dim]📋 build_report input: {input_size:,} chars "
                       f"(fixes={len(fix_summary)}, verification={'yes' if verification else 'no'}, "
                       f"plan_findings={len(plan_findings)})[/dim]")

        raw = await self._call_ai(
            system_prompt=BUILD_REPORT_SYSTEM,
            user_content=user_content,
            subagent_name="build_report",
            max_tokens=1500,
        )

        if not raw:
            return None

        # Validate all 4 sections are present
        required_sections = ["Root Cause", "Fix Applied", "Where to Implement", "Verification"]
        missing = [s for s in required_sections if s.lower() not in raw.lower()]
        if missing:
            _console.print(f"  [yellow]⚠️ build_report: missing sections: {missing} — injecting placeholders[/yellow]")
            for section in missing:
                raw += f"\n\n## {section}\n[Not available — please add manually]"

        return raw


# ═══════════════════════════════════════════════════════════════════════════════
# COMPRESS HISTORY SUBAGENT
# Replaces naive _trim_history drop
# Summarizes dropped messages into compact "session so far" preserving key findings
# ═══════════════════════════════════════════════════════════════════════════════

COMPRESS_SYSTEM = """You are a conversation compressor for a web debugging agent. Your job is to compress old conversation messages into a concise summary that preserves ALL critical information.

Produce a summary in this format:

```
SESSION PROGRESS:
- Scenario: [detected scenario]
- Key findings: [bulleted list of important discoveries]
- Fixes attempted: [what was tried and outcome]
- Current status: [where the investigation stands]
- Critical info: [iframe details, selector info, error details that MUST be preserved]
```

Rules:
1. PRESERVE all selectors, CSS properties, iframe details, error messages — these are technical and must be exact
2. PRESERVE all search results (context, playbook, fixes) — the agent won't search again
3. DROP conversational filler, repeated observations, redundant searches
4. Keep the summary under 800 tokens
5. If a fix was applied, include the EXACT code
6. Do NOT include your reasoning — only the compressed summary"""


class CompressHistorySubagent(SubagentClient):
    """Compresses old conversation messages into a concise summary."""

    async def run(self, messages_to_compress: list[dict]) -> str | None:
        """Compress messages and return summary string, or None on failure."""
        # Format messages for compression
        formatted = []
        for msg in messages_to_compress:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multimodal content — extract text parts only
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(text_parts)
            # Truncate individual messages
            if len(content) > 1500:
                content = content[:1500] + "..."
            formatted.append(f"[{role}]: {content}")

        user_content = f"""Compress these {len(formatted)} conversation messages into a concise summary:

{'---'.join(formatted)}"""

        # Log input sizes for debugging
        input_size = len(user_content)
        total_msg_chars = sum(len(m) for m in formatted)
        _console.print(f"  [dim]🗜️ compress_history input: {input_size:,} chars "
                       f"({len(formatted)} messages, {total_msg_chars:,} chars of content)[/dim]")

        raw = await self._call_ai(
            system_prompt=COMPRESS_SYSTEM,
            user_content=user_content,
            subagent_name="compress_history",
            max_tokens=1024,
        )

        return raw
