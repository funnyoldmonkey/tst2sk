"""JIT (Just-In-Time) prompting — failure-triggered contextual hints.

Instead of front-loading all guidance into the system prompt, JIT injects
targeted hints ONLY when the AI hits a known failure pattern. This keeps
the base prompt lean and delivers the right advice at the right moment.

Each rule has:
  - trigger: function(context) -> bool  — fires on specific patterns
  - hint: str — the guidance to inject
  - cooldown: int — minimum turns between re-firing the same hint
  - tag: str — unique identifier for deduplication + cooldown tracking

The engine evaluates all rules after each action result and appends
matching hints to the next context message.
"""

import re
from dataclasses import dataclass, field


@dataclass
class JITRule:
    """A single JIT rule."""
    tag: str
    hint: str
    cooldown: int = 3  # Don't re-fire same hint within N turns
    priority: int = 0  # Higher = more important (injected first)


class JITEngine:
    """Evaluates action results and injects contextual hints."""

    def __init__(self):
        self._last_fired: dict[str, int] = {}  # tag -> turn number when last fired
        self._turn: int = 0
        self._actions_used: list[str] = []  # Track all actions AI has used this session (ordered)
        self._search_results_cache: dict[str, int] = {}  # query -> result count (for broad search detection)
        self._diagnose_done: bool = False
        self._playbook_searched: bool = False
        self._fixes_searched: bool = False
        self._conversations_searched: bool = False
        self._cdp_styles_used: bool = False    # cdp_get_computed_style
        self._cdp_qsa_used: bool = False       # cdp_query_selector_all
        self._cdp_any_used: bool = False       # any CDP action
        self._context_searched: bool = False
        self._verify_fix_done: bool = False
        self._last_eval_debug: dict = {}  # Debug info from last evaluate() call

    def evaluate(
        self,
        action: str,
        payload: dict,
        result: str | dict,
        turn: int,
        scenario: str = "",
        slim_obs: dict | None = None,
    ) -> list[str]:
        """Evaluate all rules against the current action result.

        Returns a list of hint strings to inject into the next context message.
        Call this after every action (local or browser).
        """
        self._turn = turn
        self._actions_used.append(action)
        # Cap actions list to prevent unbounded growth (keep last 50)
        if len(self._actions_used) > 50:
            self._actions_used = self._actions_used[-50:]

        # Track tool usage flags
        if action == "diagnose":
            self._diagnose_done = True
            # NOTE: Do NOT auto-set _context_searched when diagnose returns product_context.
            # The AI must explicitly call search_context to set this flag.
        elif action == "investigate":
            # Investigate subagent covers diagnose + search_context + optionally playbook/fixes
            self._diagnose_done = True
            self._context_searched = True
            # Playbook/fixes flags set by brain.py based on investigation results
        elif action == "search_playbook":
            self._playbook_searched = True
        elif action == "search_fixes":
            self._fixes_searched = True
        elif action == "search_conversations":
            self._conversations_searched = True
        elif action == "search_context":
            self._context_searched = True
        elif action == "verify_fix":
            self._verify_fix_done = True
        elif action.startswith("cdp_"):
            self._cdp_any_used = True
            if action == "cdp_get_computed_style":
                self._cdp_styles_used = True
            elif action == "cdp_query_selector_all":
                self._cdp_qsa_used = True

        # Build context for rule evaluation
        ctx = _RuleContext(
            action=action,
            payload=payload,
            result=result,
            turn=turn,
            scenario=scenario,
            slim_obs=slim_obs or {},
            actions_used=self._actions_used,
            search_cache=self._search_results_cache,
            diagnose_done=self._diagnose_done,
            playbook_searched=self._playbook_searched,
            fixes_searched=self._fixes_searched,
            conversations_searched=self._conversations_searched,
            cdp_styles_used=self._cdp_styles_used,
            cdp_qsa_used=self._cdp_qsa_used,
            cdp_any_used=self._cdp_any_used,
            context_searched=self._context_searched,
            verify_fix_done=self._verify_fix_done,
        )

        # Evaluate all rules (each rule is isolated — one bad rule can't kill the engine)
        fired = []
        on_cooldown = []
        errored = []
        for rule, check_fn in _ALL_RULES:
            # Cooldown check
            last = self._last_fired.get(rule.tag, -999)
            if (turn - last) < rule.cooldown:
                remaining = rule.cooldown - (turn - last)
                on_cooldown.append((rule.tag, remaining))
                continue
            # Trigger check — isolated so one broken rule doesn't kill all JIT
            try:
                if check_fn(ctx):
                    fired.append((rule.priority, rule.hint, rule.tag))
                    self._last_fired[rule.tag] = turn
            except Exception as e:
                errored.append((rule.tag, str(e)[:80]))

        # Sort by priority (highest first)
        fired.sort(key=lambda x: -x[0])

        # Store debug info for CLI logging
        self._last_eval_debug = {
            "fired": [(tag, pri) for pri, _, tag in fired],
            "on_cooldown": on_cooldown,
            "errored": errored,
            "total_rules": len(_ALL_RULES),
        }

        # Cap at 1 hint per turn — the highest priority one only.
        # Multiple hints overwhelm small models and dilute the signal.
        return [hint for _, hint, _ in fired[:1]]

    def get_state_summary(self) -> dict:
        """Return current JIT engine state for debug logging."""
        return {
            "diagnose_done": self._diagnose_done,
            "context_searched": self._context_searched,
            "playbook_searched": self._playbook_searched,
            "fixes_searched": self._fixes_searched,
            "conversations_searched": self._conversations_searched,
            "cdp_any_used": self._cdp_any_used,
            "cdp_styles_used": self._cdp_styles_used,
            "cdp_qsa_used": self._cdp_qsa_used,
            "actions_count": len(self._actions_used),
            "last_3_actions": self._actions_used[-3:] if self._actions_used else [],
            "verify_fix_done": self._verify_fix_done,
            "rules_on_cooldown": len([t for t, _ in self._last_eval_debug.get("on_cooldown", [])]),
        }

    def get_last_eval_debug(self) -> dict:
        """Return debug info from the last evaluate() call."""
        return self._last_eval_debug


class _RuleContext:
    """Context object passed to rule trigger functions."""
    __slots__ = (
        "action", "payload", "result", "turn", "scenario", "slim_obs",
        "actions_used", "search_cache", "diagnose_done",
        "playbook_searched", "fixes_searched", "conversations_searched",
        "cdp_styles_used", "cdp_qsa_used", "cdp_any_used",
        "context_searched", "verify_fix_done",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def result_str(self) -> str:
        """Get result as string regardless of type."""
        if isinstance(self.result, str):
            return self.result
        elif isinstance(self.result, dict):
            return str(self.result)
        return ""

    @property
    def result_dict(self) -> dict:
        """Get result as dict if possible."""
        if isinstance(self.result, dict):
            return self.result
        return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RULES — each is a (JITRule, trigger_function) tuple
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ALL_RULES: list[tuple[JITRule, callable]] = []


def _rule(tag: str, hint: str, cooldown: int = 3, priority: int = 0):
    """Decorator to register a JIT rule."""
    def decorator(fn):
        _ALL_RULES.append((JITRule(tag=tag, hint=hint, cooldown=cooldown, priority=priority), fn))
        return fn
    return decorator


# ─── Search Discipline ────────────────────────────────────────────

@_rule(
    tag="broad_search_results",
    hint=(
        "JIT HINT: Your search returned too many results (50+). NARROW your query — "
        "use exact text, class names, or data attributes. "
        "Or switch to `run_test` with JS: `document.querySelectorAll('.specific-class')` "
        "Or try `cdp_query_selector_all` with a precise CSS selector for element details."
    ),
    cooldown=4,
    priority=8,
)
def _broad_search(ctx: _RuleContext) -> bool:
    if ctx.action not in ("search_dom", "search_console", "search_network"):
        return False
    rd = ctx.result_dict
    total = rd.get("total_matches", 0)
    if total >= 50:
        ctx.search_cache[rd.get("query", "")] = total
        return True
    return False


@_rule(
    tag="repeated_broad_search",
    hint=(
        "JIT HINT: You've done multiple broad searches with 50+ results. STOP searching — "
        "use `run_test` with targeted JS: "
        "`const el = document.querySelector('your-selector'); if(!el) throw 'not found'; "
        "JSON.stringify({text: el.innerText, visible: getComputedStyle(el).display !== 'none'})` "
        "Or use `cdp_query_selector_all` for batch element inspection."
    ),
    cooldown=6,
    priority=9,
)
def _repeated_broad_search(ctx: _RuleContext) -> bool:
    if ctx.action not in ("search_dom", "search_console", "search_network"):
        return False
    broad_count = sum(1 for v in ctx.search_cache.values() if v >= 50)
    return broad_count >= 2


@_rule(
    tag="empty_search_results",
    hint=(
        "JIT HINT: Your search returned 0 results. The element might use different naming. "
        "Try: 1) Broader terms (just 'cart' instead of 'add-to-cart-button'), "
        "2) `search_dom` with common patterns like 'button|submit|[type=\"submit\"]', "
        "3) `run_test` with `document.querySelectorAll('button')` to list all buttons."
    ),
    cooldown=3,
    priority=6,
)
def _empty_search(ctx: _RuleContext) -> bool:
    if ctx.action not in ("search_dom", "search_console", "search_network"):
        return False
    rd = ctx.result_dict
    return rd.get("total_matches", -1) == 0


# ─── Tool Discovery / CDP Nudges ─────────────────────────────────

@_rule(
    tag="suggest_cdp_for_styles",
    hint=(
        "JIT HINT: For precise CSS debugging, try `cdp_get_computed_style` — it returns "
        "the full computed style from Chrome's CSS engine (more accurate than inspect_element). "
        "Usage: {\"action\": \"cdp_get_computed_style\", \"payload\": {\"selector\": \".your-element\"}}"
    ),
    cooldown=8,
    priority=5,
)
def _suggest_cdp_styles(ctx: _RuleContext) -> bool:
    # Fire after 2+ inspect_element calls without using CDP
    if ctx.action != "inspect_element":
        return False
    inspect_count = ctx.actions_used.count("inspect_element")
    return inspect_count >= 2 and not ctx.cdp_styles_used


@_rule(
    tag="suggest_cdp_cookies",
    hint=(
        "JIT HINT: For auth/session debugging, use `cdp_get_cookies` to see all cookies "
        "including httpOnly ones (invisible to JS). Useful for session state, auth tokens, "
        "and consent cookies. Usage: {\"action\": \"cdp_get_cookies\", \"payload\": {}}"
    ),
    cooldown=15,
    priority=4,
)
def _suggest_cdp_cookies(ctx: _RuleContext) -> bool:
    # Fire when investigating auth/session issues
    if ctx.scenario not in ("auth_flow",):
        return False
    return not ctx.cdp_any_used and ctx.turn >= 3


@_rule(
    tag="suggest_cdp_metrics",
    hint=(
        "JIT HINT: Page seems slow or complex. Use `cdp_get_page_metrics` to check "
        "node count, JS heap size, layout/recalc duration, and event listener count. "
        "This reveals performance bottlenecks invisible to DOM inspection."
    ),
    cooldown=15,
    priority=3,
)
def _suggest_cdp_metrics(ctx: _RuleContext) -> bool:
    # Fire when DOM is very large (900+ elements)
    obs = ctx.slim_obs
    dom = obs.get("dom", {})
    total = dom.get("total_elements", 0)
    return total >= 900 and not ctx.cdp_any_used


@_rule(
    tag="suggest_cdp_queryselector",
    hint=(
        "JIT HINT: Instead of search_dom (text grep), try `cdp_query_selector_all` — it runs "
        "a real CSS selector and returns each element's visibility, bounding rect, and disabled state. "
        "Much more precise for finding specific UI elements. "
        "Usage: {\"action\": \"cdp_query_selector_all\", \"payload\": {\"selector\": \"button[type=submit]\"}}"
    ),
    cooldown=8,
    priority=6,
)
def _suggest_cdp_queryselector(ctx: _RuleContext) -> bool:
    # Fire when search_dom has been used 3+ times without CDP
    if ctx.action != "search_dom":
        return False
    search_dom_count = ctx.actions_used.count("search_dom")
    return search_dom_count >= 3 and not ctx.cdp_qsa_used


# ─── Missing Tool Usage ──────────────────────────────────────────

@_rule(
    tag="missing_diagnose",
    hint=(
        "⛔ STOP — You MUST run `investigate` before fixing anything. It's a specialized subagent "
        "that reads ALL data sources (DOM, console, network, context docs, playbooks, past fixes) "
        "and returns a structured briefing with scenario, critical info, and recommended approach. "
        "This replaces diagnose + search_context + search_playbook + search_fixes in ONE call. "
        "Run `investigate` NOW with keywords from the user's query."
    ),
    cooldown=2,
    priority=11,  # Higher than all other rules — investigate is non-negotiable
)
def _missing_diagnose(ctx: _RuleContext) -> bool:
    return ctx.turn >= 2 and not ctx.diagnose_done


@_rule(
    tag="missing_playbook",
    hint=(
        "JIT HINT: You haven't searched the playbook yet. Use `search_playbook` with "
        "symptom keywords — it contains step-by-step recipes for common issues. "
        "Also try `search_fixes` to check past verified fixes for similar problems."
    ),
    cooldown=10,
    priority=7,
)
def _missing_playbook(ctx: _RuleContext) -> bool:
    # Fire after turn 4 if no playbook/fixes searched
    return ctx.turn >= 4 and not ctx.playbook_searched and not ctx.fixes_searched


@_rule(
    tag="missing_conversations",
    hint=(
        "JIT HINT: You haven't checked past session transcripts. Use `search_conversations` "
        "with keywords — it searches the tag index across all past sessions. If you find a "
        "match, use `get_conversation_detail` to see what worked before."
    ),
    cooldown=15,
    priority=3,
)
def _missing_conversations(ctx: _RuleContext) -> bool:
    # Fire after turn 8 if playbook was searched but conversations weren't
    return ctx.turn >= 8 and ctx.playbook_searched and not ctx.conversations_searched


# ─── Context Folder Awareness ────────────────────────────────────

@_rule(
    tag="fix_without_context",
    hint=(
        "⛔ STOP — You are injecting code WITHOUT checking product context first. "
        "The context/ folder has critical info: iframe rendering (BIS modal is inside #BIS_frame iframe — "
        "inject_css WON'T WORK, you need inject_js targeting frame.contentDocument), DOM selectors, "
        "scoping rules, and known fix patterns. "
        "Run `search_context` NOW with keywords like 'BIS', 'slide cart', 'modal', 'iframe', 'drawer' "
        "BEFORE your next inject. Skipping this leads to wasted turns fixing the wrong thing."
    ),
    cooldown=4,
    priority=10,
)
def _fix_without_context(ctx: _RuleContext) -> bool:
    """Fire when AI injects CSS/JS without checking context — high priority blocker.

    Does NOT require diagnose_done — if context files exist and the AI is injecting
    code, it should have checked context regardless of whether diagnose was run.
    """
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    if ctx.context_searched:
        return False
    import os
    if not os.path.isdir("context") or not any(f.endswith(".md") for f in os.listdir("context")):
        return False
    return True


@_rule(
    tag="inspect_before_fix",
    hint=(
        "⚠️ You are injecting a fix WITHOUT inspecting the target element first. "
        "BEFORE any inject_css or inject_js, you MUST verify the selector exists using "
        "`inspect_element`, `cdp_query_selector_all`, or `run_test` with querySelector. "
        "Guessing selectors wastes turns — inspect first, then fix what you KNOW exists."
    ),
    cooldown=2,
    priority=10,
)
def _inspect_before_fix(ctx: _RuleContext) -> bool:
    """Fires when AI injects CSS/JS without prior inspection of the target."""
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    inspection_actions = {
        "inspect_element", "cdp_query_selector_all", "cdp_get_computed_style",
        "cdp_get_matched_styles", "cdp_get_event_listeners", "search_all_frames",
        "cdp_get_network_details", "run_test", "search_dom"
    }
    recent = ctx.actions_used[-6:-1] if len(ctx.actions_used) > 5 else ctx.actions_used[:-1]
    has_inspection = any(a in inspection_actions for a in recent)
    return not has_inspection


@_rule(
    tag="no_css_in_iframe",
    hint=(
        "⛔ WRONG APPROACH: You are using `inject_css` to style an element INSIDE an IFRAME. "
        "CSS injected into the main page CANNOT reach inside an iframe. You MUST use `inject_js` "
        "to inject a <style> tag into the iframe's contentDocument. Example:\n"
        "(function() {\n"
        "  const frame = document.getElementById('BIS_frame');\n"
        "  if (frame && frame.contentDocument) {\n"
        "    const style = frame.contentDocument.createElement('style');\n"
        "    style.textContent = '/* your CSS here */';\n"
        "    frame.contentDocument.head.appendChild(style);\n"
        "  }\n"
        "})();"
    ),
    cooldown=3,
    priority=10,
)
def _no_css_in_iframe(ctx: _RuleContext) -> bool:
    """Fires ONLY when inject_css targets selectors known to be inside an iframe.

    Previous version was too broad — it fired whenever inject_js had been used before,
    even when the CSS targeted main-document elements like .amp-buy-x-get-y-bundles or
    #ProductSubmitButton. This caused false positives that confused the AI into
    second-guessing correct CSS fixes.

    Now it checks the actual CSS payload for selectors that reference iframe-interior
    elements (e.g., #BIS_frame content, .bis-modal internals, .back-in-stock form elements).
    Elements that merely have "bis" or "BIS" in their class name but live in the main
    document (like .bis-button.BIS_trigger) do NOT trigger this rule.
    """
    if ctx.action != "inject_css":
        return False

    css = ctx.payload.get("css", "").lower()
    if not css:
        return False

    # Only fire when CSS targets selectors KNOWN to be inside an iframe.
    # These are elements that live inside #BIS_frame's contentDocument:
    iframe_interior_selectors = (
        "#bis_frame",           # Targeting the iframe itself (CSS can't style its contents)
        ".bis-modal",           # The modal container inside the iframe
        ".bis-content",         # Content wrapper inside iframe
        ".bis-form",            # Form inside iframe
        "#bis-email",           # Email input inside iframe
        ".bis-submit",          # Submit button inside iframe
        "#bis-popup",           # Popup inside iframe
        ".bis_modal",           # Alternate naming
        "iframe#bis",           # Direct iframe targeting
        "#backinstockform",     # BIS form inside iframe
    )

    # Check if any of the CSS selectors target iframe-interior elements
    return any(sel in css for sel in iframe_interior_selectors)


@_rule(
    tag="context_available_after_diagnose",
    hint=(
        "⛔ MANDATORY: You just ran diagnose and product context files are available, but you "
        "haven't searched them yet. Run `search_context` NOW with the product/app name "
        "(e.g., 'BIS', 'back in stock', 'slide cart', 'modal', 'iframe'). "
        "Context files contain CRITICAL info like: BIS modal renders inside #BIS_frame iframe "
        "(inject_css won't work!), exact CSS selectors, scoping rules, and known fix patterns. "
        "Skipping this step is the #1 cause of wasted turns. DO IT NOW before any fix attempt."
    ),
    cooldown=6,
    priority=10,
)
def _context_available_after_diagnose(ctx: _RuleContext) -> bool:
    """Nudge AI to search context right after diagnose, before it starts fixing."""
    if ctx.action != "diagnose":
        return False
    if ctx.context_searched:
        return False
    import os
    if not os.path.isdir("context") or not any(f.endswith(".md") for f in os.listdir("context")):
        return False
    return True


# ─── Fix Attempt Guidance ─────────────────────────────────────────

@_rule(
    tag="css_before_js",
    hint=(
        "JIT HINT: ESCALATION ORDER — You're using inject_js, but did you try inject_css first? "
        "Most visibility/layout issues can be fixed with CSS alone (display, opacity, visibility, "
        "z-index, pointer-events). JS should be Level 2 — only for event handlers, form logic, "
        "variant IDs, fetch/API, or script re-initialization."
    ),
    cooldown=6,
    priority=8,
)
def _css_before_js(ctx: _RuleContext) -> bool:
    if ctx.action != "inject_js":
        return False
    # Only fire if no inject_css has been used yet
    if "inject_css" in ctx.actions_used:
        return False
    # DON'T fire if the JS targets an iframe — inject_css CANNOT reach inside iframes,
    # so inject_js is the ONLY correct approach (e.g., BIS modal inside #BIS_frame)
    code = ctx.payload.get("code", "").lower()
    iframe_signals = ("contentdocument", "contentwindow", "bis_frame", "iframe", "bismodal")
    if any(sig in code for sig in iframe_signals):
        return False
    return True


@_rule(
    tag="fix_without_verify",
    hint=(
        "JIT HINT: You applied a fix but haven't verified it yet. MANDATORY after every fix: "
        "1) INTERACT — `click` the fixed element, `type` in inputs, `scroll` to check layout. Prove a real user can use it. "
        "2) OBSERVE — Look at what changed after interaction. "
        "3) ASSERT — Run `run_test` with getComputedStyle/getBoundingClientRect to confirm specific values. "
        "All three steps are required. A passing run_test WITHOUT clicking is NOT verification."
    ),
    cooldown=4,
    priority=9,
)
def _fix_without_verify(ctx: _RuleContext) -> bool:
    # Fire if current action is inject AND there was a PREVIOUS inject with no run_test between them
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    # Check actions BEFORE the current one (exclude last element which is current action)
    actions = ctx.actions_used[:-1] if len(ctx.actions_used) > 1 else []
    prev_inject_idx = -1
    last_verify_idx = -1
    for i, a in enumerate(actions):
        if a in ("inject_css", "inject_js"):
            prev_inject_idx = i
        if a == "run_test":
            last_verify_idx = i
    # Fire only if there was a previous inject and no run_test after it
    return prev_inject_idx >= 0 and last_verify_idx < prev_inject_idx


@_rule(
    tag="must_verify_before_report",
    hint=(
        "⛔ STOP — You MUST run `verify_fix` before reporting to the user. "
        "verify_fix is a comprehensive subagent that runs REAL browser checks on ALL your fixes: "
        "computed styles, element visibility, dimensions, iframe state, and regressions. "
        "It's the final quality gate — without it, you might report a broken fix. "
        "Call `verify_fix` NOW. If it fails, go back and fix the issues before reporting."
    ),
    cooldown=2,
    priority=11,  # Highest priority — blocks reporting
)
def _must_verify_before_report(ctx: _RuleContext) -> bool:
    """Fires when AI tries to report/build_report without running verify_fix first."""
    if ctx.action not in ("post_message", "answer_user", "build_report"):
        return False
    # Don't fire if no fixes were applied (nothing to verify)
    fix_actions = {"inject_css", "inject_js"}
    has_fixes = any(a in fix_actions for a in ctx.actions_used)
    if not has_fixes:
        return False
    # Fire if verify_fix hasn't been run
    return not ctx.verify_fix_done


@_rule(
    tag="run_test_failed",
    hint=(
        "JIT HINT: Your run_test failed or returned no useful data. Common issues: "
        "1) Selector doesn't match — double-check with search_dom or cdp_query_selector_all, "
        "2) Element exists but computed style differs from expected — check parent chain, "
        "3) Timing issue — element might not be rendered yet. "
        "Try `cdp_get_computed_style` for the element to see its actual current state."
    ),
    cooldown=4,
    priority=7,
)
def _run_test_failed(ctx: _RuleContext) -> bool:
    if ctx.action != "run_test":
        return False
    rs = ctx.result_str.lower()
    return "success: false" in rs or "failed" in rs or "error" in rs


# ─── Observation-Based Guidance ───────────────────────────────────

@_rule(
    tag="hidden_elements_hint",
    hint=(
        "JIT HINT: The observation shows hidden interactive elements with traced causes. "
        "Check the `visibility_analysis` field — it tells you exactly WHY each element is hidden "
        "(parent display:none, overflow:hidden, z-index, etc). Fix the ROOT cause in the parent, "
        "not the element itself."
    ),
    cooldown=10,
    priority=6,
)
def _hidden_elements(ctx: _RuleContext) -> bool:
    obs = ctx.slim_obs
    vis = obs.get("visibility_analysis", {})
    count = vis.get("hidden_interactive_count", 0)
    return count >= 3 and ctx.turn <= 5  # Only early in investigation


@_rule(
    tag="api_insights_available",
    hint=(
        "JIT HINT: Auto-parsed API data is available in `api_insights`. Check it — "
        "it may already tell you the cart state, product availability, variant info, "
        "or API errors without needing to manually call read_network_body."
    ),
    cooldown=15,
    priority=5,
)
def _api_insights(ctx: _RuleContext) -> bool:
    obs = ctx.slim_obs
    return "api_insights" in obs and ctx.turn <= 6


@_rule(
    tag="no_changes_after_fix",
    hint=(
        "JIT HINT: Your last action produced NO changes in the DOM/console/network. "
        "Your fix might not be taking effect. Possible reasons: "
        "1) CSS specificity — your rule is being overridden (add !important), "
        "2) JS re-applying styles — your CSS fix gets clobbered by scripts, "
        "3) Wrong selector — your fix targets an element that doesn't exist. "
        "Use `cdp_get_computed_style` to check if your styles actually applied."
    ),
    cooldown=4,
    priority=9,
)
def _no_changes_after_fix(ctx: _RuleContext) -> bool:
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    obs = ctx.slim_obs
    changes = obs.get("changes_since_last_turn", [])
    if isinstance(changes, list) and len(changes) == 1 and "No significant changes" in str(changes[0]):
        return True
    return False


# ─── Shopify-Specific Guidance ────────────────────────────────────

@_rule(
    tag="shopify_atc_hint",
    hint=(
        "JIT HINT: This is a Shopify store. Common ATC button selectors: "
        "[type=\"submit\"], .product-form__submit, .btn--add-to-cart, [name=\"add\"], "
        "form[action*=\"/cart/add\"]. Also check if a variant must be selected first — "
        "the button is often disabled until a variant is chosen. "
        "Use `cdp_query_selector_all` with `form[action*=\"/cart/add\"] button` for precise matching."
    ),
    cooldown=15,
    priority=5,
)
def _shopify_atc(ctx: _RuleContext) -> bool:
    if "shopify" not in ctx.scenario:
        return False
    # Fire when searching for cart/add/button on Shopify
    if ctx.action != "search_dom":
        return False
    query = ctx.payload.get("query", "").lower()
    return any(k in query for k in ["cart", "add", "button", "submit"])


@_rule(
    tag="shopify_variant_hint",
    hint=(
        "JIT HINT: On Shopify product pages, variant selection is critical. "
        "The product JSON is at /products/HANDLE.json — it lists all variants with IDs and availability. "
        "Check `api_insights` if available, or use `read_network_body` with 'products' to inspect it. "
        "Hidden variant selectors often use: input[name=\"id\"], select.product-form__variants, "
        "[data-variant-id], .swatch--size."
    ),
    cooldown=15,
    priority=4,
)
def _shopify_variant(ctx: _RuleContext) -> bool:
    if "shopify" not in ctx.scenario:
        return False
    if ctx.action != "search_dom":
        return False
    query = ctx.payload.get("query", "").lower()
    return any(k in query for k in ["variant", "size", "color", "option", "swatch"])


@_rule(
    tag="selector_not_found",
    hint=(
        "JIT HINT: Selector not found. The element may use a different selector than expected. "
        "Try: 1) `search_dom` with partial text content, "
        "2) `cdp_query_selector_all` with a broader CSS selector (e.g., 'button' instead of '.specific-class'), "
        "3) `run_test` with `document.querySelectorAll('*')` filtered by innerText. "
        "Also check if the element is inside a shadow DOM or iframe."
    ),
    cooldown=3,
    priority=7,
)
def _selector_not_found(ctx: _RuleContext) -> bool:
    if ctx.action not in ("click", "hover", "inspect_element", "capture_element", "type"):
        return False
    return "not found" in ctx.result_str.lower()


# ─── Batch Plan Completion ────────────────────────────────────────

@_rule(
    tag="complete_task_before_moving_on",
    hint=(
        "⚠️ PLAN UPDATE OVERDUE: You've done 10+ actions without updating your plan. "
        "Use `update_plan` with `complete_all` to batch-mark finished tasks with findings. "
        "Example: `{\"complete_all\": [{\"index\": 0, \"findings\": \"...\"}, {\"index\": 1, \"findings\": \"...\"}]}` "
        "The completion gate will BLOCK `post_message` if tasks are still incomplete, so "
        "batch-update your plan at major milestones (after investigation, after fixes, before reporting)."
    ),
    cooldown=6,
    priority=8,
)
def _complete_task_before_moving_on(ctx: _RuleContext) -> bool:
    """Fire when AI hasn't updated the plan in a long time despite doing real work.

    Aligned with prompt guidance: "Batch plan updates at major milestones using
    complete_all to conserve turns." Only fires after 10+ actions without any
    update_plan call, AND at least one fix+verify cycle has happened in that span.
    This avoids nagging after every single fix→verify pair.
    """
    if ctx.action == "update_plan":
        return False  # They're doing the right thing
    if ctx.action in ("observe", "post_message", "answer_user"):
        return False

    # Count actions since last update_plan
    actions_since_plan = []
    for a in reversed(ctx.actions_used[:-1]):
        if a == "update_plan":
            break
        actions_since_plan.append(a)

    # Only fire after 10+ actions without a plan update
    if len(actions_since_plan) < 10:
        return False

    # And only if real work happened (at least one fix + one verify)
    fix_actions = {"inject_css", "inject_js"}
    verify_actions = {
        "run_test", "inspect_element", "cdp_get_computed_style",
        "cdp_get_matched_styles", "cdp_get_event_listeners", "search_all_frames", "cdp_get_network_details"
    }
    has_fix = any(a in fix_actions for a in actions_since_plan)
    has_verify = any(a in verify_actions for a in actions_since_plan)
    return has_fix and has_verify


# ─── Task Completion / Runaway Prevention ─────────────────────────

@_rule(
    tag="answer_ready_no_postmessage",
    hint=(
        "JIT HINT: You seem to have the information needed to answer the user, "
        "but you haven't used `post_message` yet. If you've completed the investigation "
        "or answered what the user asked, use `post_message` NOW to deliver your findings. "
        "Don't keep investigating after you already have the answer."
    ),
    cooldown=3,
    priority=10,
)
def _answer_ready_no_postmessage(ctx: _RuleContext) -> bool:
    # Fire when the AI uses 'observe' but hasn't used post_message.
    # Only fire at turn >= 8 to avoid interrupting normal early investigation.
    if ctx.action != "observe":
        return False
    return "post_message" not in ctx.actions_used and "answer_user" not in ctx.actions_used and ctx.turn >= 8


@_rule(
    tag="excessive_observe",
    hint=(
        "JIT HINT: You've used `observe` multiple times without taking action. "
        "Observing repeatedly gives you the same page state. Either: "
        "1) Take an action (search, inject, click), "
        "2) Use `post_message` to report what you've found, or "
        "3) Use `diagnose` if you haven't already."
    ),
    cooldown=4,
    priority=8,
)
def _excessive_observe(ctx: _RuleContext) -> bool:
    if ctx.action != "observe":
        return False
    # Count CONSECUTIVE observes at the end of the action list
    consecutive = 0
    for a in reversed(ctx.actions_used):
        if a == "observe":
            consecutive += 1
        else:
            break
    return consecutive >= 2


@_rule(
    tag="too_many_searches_no_action",
    hint=(
        "JIT HINT: You've done 8+ search/diagnostic actions without attempting a fix or reporting to the user. "
        "It's time to either: "
        "1) Attempt a fix based on what you've learned (inject_css or inject_js), "
        "2) Use `post_message` to report your findings, or "
        "3) Move on to the next item if you're stuck on this one."
    ),
    cooldown=6,
    priority=9,
)
def _too_many_searches(ctx: _RuleContext) -> bool:
    search_actions = {"search_dom", "search_console", "search_network", "search_playbook",
                      "search_fixes", "search_conversations", "diagnose", "inspect_element",
                      "read_network_body", "get_conversation_detail"}
    fix_actions = {"inject_css", "inject_js", "post_message", "answer_user"}
    search_count = sum(1 for a in ctx.actions_used if a in search_actions)
    fix_count = sum(1 for a in ctx.actions_used if a in fix_actions)
    return search_count >= 8 and fix_count == 0


# ─── Repeated Failures ───────────────────────────────────────────

@_rule(
    tag="repeated_click_failures",
    hint=(
        "JIT HINT: Multiple click actions have failed (selector not found). "
        "The page structure may be different from what you expect. "
        "Use `cdp_query_selector_all` with 'button' or '[role=button]' to see ALL clickable elements "
        "with their text, visibility, and coordinates. Then use `click_at_position` as a fallback."
    ),
    cooldown=5,
    priority=8,
)
def _repeated_click_failures(ctx: _RuleContext) -> bool:
    if ctx.action != "click":
        return False
    if "not found" not in ctx.result_str.lower():
        return False
    # Check if there was a previous click failure
    click_indices = [i for i, a in enumerate(ctx.actions_used) if a == "click"]
    return len(click_indices) >= 2


@_rule(
    tag="inject_js_error",
    hint=(
        "JIT HINT: Your inject_js threw an exception. Check the error message — common causes: "
        "1) querySelector returned null — the element doesn't exist at that selector, "
        "2) CSP might be blocking despite CDP bypass — try wrapping in a try/catch, "
        "3) The code references a variable/function that doesn't exist on this page. "
        "Use `cdp_query_selector_all` to verify the element exists before injecting."
    ),
    cooldown=3,
    priority=8,
)
def _inject_js_error(ctx: _RuleContext) -> bool:
    if ctx.action != "inject_js":
        return False
    return "exception" in ctx.result_str.lower() or "[error]" in ctx.result_str.lower()


# ─── CDP-Specific Guidance ────────────────────────────────────────

@_rule(
    tag="cdp_scratch_hint",
    hint=(
        "JIT HINT: Full CDP results are saved to scratch/ files. If the in-message data "
        "was truncated, you can search the full data: "
        "scratch/cdp_dom_tree.json, scratch/cdp_cookies.json, scratch/cdp_computed_style.json, "
        "scratch/cdp_query_results.json, scratch/cdp_page_metrics.json."
    ),
    cooldown=20,
    priority=3,
)
def _cdp_scratch_hint(ctx: _RuleContext) -> bool:
    if not ctx.action.startswith("cdp_"):
        return False
    return "TRUNCATED" in ctx.result_str or "truncated" in ctx.result_str


@_rule(
    tag="investigate_after_navigate",
    hint=(
        "JIT HINT: You navigated to a new page. Start with `diagnose` to detect the scenario, "
        "then check the observation data (visibility_analysis, api_insights, changes_since_last_turn) "
        "before jumping into searches."
    ),
    cooldown=10,
    priority=6,
)
def _investigate_after_navigate(ctx: _RuleContext) -> bool:
    if ctx.action != "navigate":
        return False
    return not ctx.diagnose_done


# ─── Smart Escalation ────────────────────────────────────────────

@_rule(
    tag="try_dom_reconstruction",
    hint=(
        "JIT HINT: You've tried multiple CSS and JS fixes without success. "
        "Consider Level 3: DOM reconstruction — rebuild the broken element from scratch using inject_js. "
        "Or escalate to Level 4: use `post_message` to tell the user the root cause and what needs manual fixing."
    ),
    cooldown=10,
    priority=7,
)
def _try_dom_reconstruction(ctx: _RuleContext) -> bool:
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    fix_count = sum(1 for a in ctx.actions_used if a in ("inject_css", "inject_js"))
    return fix_count >= 4  # After 4+ fix attempts


@_rule(
    tag="changes_detected_verify",
    hint=(
        "JIT HINT: Changes were detected after your fix! DOM/console/network shifted. "
        "Now INTERACT with the fixed element — `click` it, `type` in it, `scroll` to it — "
        "then `run_test` to assert the values. Don't skip the interaction step."
    ),
    cooldown=4,
    priority=7,
)
def _changes_detected_verify(ctx: _RuleContext) -> bool:
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    obs = ctx.slim_obs
    changes = obs.get("changes_since_last_turn", [])
    if not changes:
        return False
    # Fire if changes detected AND they're not "no changes"
    has_real_changes = any("No significant changes" not in str(c) for c in changes)
    # Only fire if run_test hasn't been used after the last inject
    actions = ctx.actions_used
    last_inject = len(actions) - 1 - actions[::-1].index(ctx.action) if ctx.action in actions else -1
    last_test = -1
    for i, a in enumerate(actions):
        if a == "run_test":
            last_test = i
    return has_real_changes and last_test < last_inject


# ─── Data Retrieval ──────────────────────────────────────────────

@_rule(
    tag="run_test_no_return",
    hint=(
        "JIT HINT: Your `run_test` returned 'Test Passed' but no data. "
        "To GET DATA BACK from run_test, use `return` in your code! Example: "
        '`return document.querySelectorAll("button").length` or '
        '`return Array.from(document.querySelectorAll("form")).map(f => ({action: f.action, id: f.id}))`. '
        "The returned value appears in the result's \"data\" field. "
        "Don't use console.log() — use return!"
    ),
    cooldown=3,
    priority=9,
)
def _run_test_no_return(ctx: _RuleContext) -> bool:
    """Fires when run_test keeps returning Test Passed without data — AI is using console.log instead of return."""
    if ctx.action != "run_test":
        return False
    result_str = ctx.result_str
    # Fire if result says "Test Passed" but has no "data" field
    if "Test Passed" in result_str and "'data'" not in result_str and '"data"' not in result_str:
        # Only fire if 2+ run_tests without data
        recent_tests = [a for a in ctx.actions_used[-6:] if a == "run_test"]
        return len(recent_tests) >= 2
    return False


@_rule(
    tag="too_many_tests_no_interaction",
    hint=(
        "JIT HINT: You've run 4+ tests/searches without clicking, scrolling, or interacting with the page. "
        "If you're trying to find elements, use `run_test` with `return` to get data back, then "
        "ACT on what you find — click buttons, scroll to elements, or start fixing issues. "
        "Investigation without interaction is spinning your wheels."
    ),
    cooldown=5,
    priority=8,
)
def _too_many_tests_no_interaction(ctx: _RuleContext) -> bool:
    """Fires when AI does 4+ non-interactive actions in a row (search/test/observe only)."""
    interactive = {"click", "type", "scroll", "hover", "inject_css", "inject_js",
                   "navigate", "click_at_position", "post_message", "answer_user"}
    recent = ctx.actions_used[-4:]
    if len(recent) < 4:
        return False
    return not any(a in interactive for a in recent)


# ─── Overlay / Drawer / Modal Blocking ───────────────────────────

@_rule(
    tag="overlay_blocking",
    hint=(
        "JIT HINT: You may have an OVERLAY, CART DRAWER, or MODAL blocking the page. "
        "Common blockers: cart drawers that open after Add to Cart, cookie banners, "
        "signup modals, notification popups. BEFORE continuing your investigation:\n"
        "1) Use `run_test` to detect overlays: "
        "`return Array.from(document.querySelectorAll('[class*=\"drawer\"], [class*=\"modal\"], "
        "[class*=\"overlay\"], [class*=\"popup\"], [class*=\"cart-drawer\"], [class*=\"slide\"]'))"
        ".filter(el => { const s = getComputedStyle(el); return s.display !== 'none' && "
        "s.visibility !== 'hidden' && parseFloat(s.opacity) > 0; })"
        ".map(el => ({ tag: el.tagName, classes: el.className, id: el.id }))`\n"
        "2) Close the overlay: click its close button (X, ✕, 'Continue Shopping', 'Close'), "
        "click outside it, or press Escape via `inject_js`: "
        "`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))`\n"
        "3) THEN resume your original investigation with a fresh observe."
    ),
    cooldown=8,
    priority=9,
)
def _overlay_blocking(ctx: _RuleContext) -> bool:
    """Fires when AI can't find expected elements and an overlay might be blocking.

    Triggers when: 3+ run_test/search_dom in last 5 actions return empty/null/not found,
    OR the AI just did a click that likely opened an overlay (add to cart, etc.)
    and is now struggling to find elements.
    """
    if ctx.turn < 3:
        return False

    # Check if recent actions suggest struggling to find elements
    recent = ctx.actions_used[-5:] if len(ctx.actions_used) >= 5 else ctx.actions_used
    search_actions = [a for a in recent if a in ("run_test", "search_dom", "inspect_element")]
    if len(search_actions) < 3:
        return False

    # Check result for signs of "not found" / empty results
    result_str = ctx.result_str.lower()
    not_found_signals = (
        "null" in result_str
        or "not found" in result_str
        or "undefined" in result_str
        or "[]" in result_str
        or "0 results" in result_str
        or "no elements" in result_str
        or "length: 0" in result_str
        or "no matches" in result_str
    )

    if not not_found_signals:
        return False

    # Check if a cart/overlay-opening action happened recently
    # (click on add-to-cart, or any click in the last 8 actions)
    had_recent_click = "click" in ctx.actions_used[-8:] or "click_at_position" in ctx.actions_used[-8:]

    return had_recent_click


@_rule(
    tag="close_overlay_before_continue",
    hint=(
        "JIT HINT: After clicking Add to Cart (or similar), a CART DRAWER or OVERLAY "
        "likely opened and is covering the page. You MUST close it before looking for "
        "other elements. Try:\n"
        "1) `run_test`: `return document.querySelector('[class*=\"drawer\"] button[class*=\"close\"], "
        "[class*=\"drawer\"] [aria-label*=\"close\"], [class*=\"cart\"] .close-btn, "
        "button[aria-label=\"Close\"]')?.outerHTML`\n"
        "2) If found, `click` that close button selector.\n"
        "3) If no close button, try: `inject_js` with "
        "`document.querySelector('[class*=\"drawer\"], [class*=\"cart-drawer\"]')"
        ".style.display = 'none'`\n"
        "4) After closing, do a fresh `observe` to see the page clearly again."
    ),
    cooldown=10,
    priority=10,
)
def _close_overlay_before_continue(ctx: _RuleContext) -> bool:
    """Fires specifically when the slim_obs shows potential overlay in DOM.

    Looks at the slim observation for drawer/modal/overlay keywords in
    the DOM summary — if present AND the AI has been struggling, fire.
    """
    if ctx.turn < 4:
        return False

    slim = ctx.slim_obs
    if not slim:
        return False

    # Check DOM for overlay-related content
    dom_summary = str(slim.get("dom", "")).lower()
    overlay_keywords = ("drawer", "modal", "overlay", "cart-drawer", "slide-in",
                        "popup", "lightbox", "cart_drawer")
    has_overlay_in_dom = any(kw in dom_summary for kw in overlay_keywords)

    if not has_overlay_in_dom:
        return False

    # Only fire if AI seems stuck (3+ non-click actions recently)
    recent = ctx.actions_used[-4:] if len(ctx.actions_used) >= 4 else ctx.actions_used
    non_interactive = [a for a in recent if a in ("run_test", "search_dom", "observe", "inspect_element")]
    return len(non_interactive) >= 3


# ── Rule: Use browser interaction instead of just run_test ──────────────
@_rule(
    tag="create_plan_first",
    hint=(
        "JIT HINT: You haven't created a plan yet! Your FIRST action should always be "
        "`update_plan` — read the task, break it into steps, then execute. "
        "Do it NOW before your next investigation step. Example: `update_plan` with "
        "`{ \"tasks\": [\"Run diagnose\", \"Investigate root cause\", \"Search playbook/KB\", "
        "\"Apply fix\", \"Verify fix\", \"Report findings\"] }`"
    ),
    cooldown=8,
    priority=10,
)
def _create_plan_first(ctx: _RuleContext) -> bool:
    """Fires immediately if AI takes any action without creating a plan first."""
    # Only fire if AI hasn't used update_plan yet
    return "update_plan" not in ctx.actions_used


# ─── Guidance moved from system prompt → JIT ────────────────────

@_rule(
    tag="console_error_verification",
    hint=(
        "⚠️ CONSOLE ERROR VERIFICATION: Diagnose found SUSPICIOUS console errors — they may be "
        "planted or misleading. Do NOT trust them blindly. Cross-reference each error with the DOM: "
        "1) Does the referenced script/module appear in obs_dom.txt `📜 [script]` entries? "
        "2) Can you reproduce the error by interacting with the element? "
        "3) Does the error correlate with a failed network request? "
        "Only trust errors classified as 'real' (JS exceptions, resource failures). "
        "Suspicious errors need DOM evidence before acting on them."
    ),
    cooldown=8,
    priority=9,
)
def _console_error_verification(ctx: _RuleContext) -> bool:
    """Fires after diagnose when suspicious console errors are found."""
    if ctx.action != "diagnose":
        return False
    rd = ctx.result_dict
    classification = rd.get("console_error_classification", {})
    return classification.get("total_suspicious", 0) > 0



@_rule(
    tag="delivering_fix_format",
    hint=(
        "⚠️ REPORT FORMAT: Your post_message MUST include ALL of these:\n"
        "1) **Root Cause** — what was broken and why\n"
        "2) **Fix Code** — the exact CSS or JS that fixes it (ready to copy-paste)\n"
        "3) **Where to Implement** — which theme file, section, or app setting\n"
        "4) Last line MUST be: `Type Summarize to save fixes, then End to close.`\n"
        "If any section is missing, the merchant can't act on your fix. Include all four."
    ),
    cooldown=3,
    priority=10,
)
def _delivering_fix_format(ctx: _RuleContext) -> bool:
    """Fires on post_message to ensure the report has the required format."""
    if ctx.action != "post_message":
        return False
    msg = ctx.payload.get("message", "").lower()
    has_root_cause = "root cause" in msg or "cause:" in msg or "problem:" in msg
    has_fix_code = "```" in msg or "inject_css" in msg or "inject_js" in msg or "css" in msg
    has_where = "theme" in msg or "implement" in msg or "file" in msg or "section" in msg or "setting" in msg
    has_closing = "summarize" in msg or "end to close" in msg
    # Fire if missing 2+ required sections
    missing = sum(1 for x in [has_root_cause, has_fix_code, has_where, has_closing] if not x)
    return missing >= 2


@_rule(
    tag="interact_dont_just_test",
    hint=(
        "JIT HINT: You've been using run_test/search without actually INTERACTING with the browser. "
        "You are a browser agent — use `click`, `scroll`, `type`, `hover` to test elements like a real user. "
        "run_test is for ASSERTING values, not for testing if buttons work. "
        "To verify an element: CLICK it → observe what changed → THEN run_test to assert. "
        "Never conclude 'element works' without clicking it first."
    ),
    cooldown=6,
    priority=9,
)
def _interact_dont_just_test(ctx: _RuleContext) -> bool:
    """Fires when AI uses run_test/search 4+ times in a row without any click/scroll/type/hover."""
    if ctx.turn < 4:
        return False

    browser_actions = {"click", "scroll", "type", "hover", "click_at_position"}
    recent = ctx.actions_used[-5:] if len(ctx.actions_used) >= 5 else ctx.actions_used

    # If no browser interaction in last 5 actions and at least 3 are run_test/search
    has_interaction = any(a in browser_actions for a in recent)
    test_or_search = [a for a in recent if a in ("run_test", "search_dom", "search_console", "search_network", "inspect_element", "diagnose")]

    return not has_interaction and len(test_or_search) >= 3


# ── Rule: Must INTERACT with fixed element before verifying or reporting ──
@_rule(
    tag="verify_without_interact",
    hint=(
        "JIT HINT: You applied a fix and jumped straight to run_test/post_message WITHOUT "
        "interacting with the fixed element first. The fix-verify loop REQUIRES: "
        "interact → observe → assert. You MUST `click`, `scroll`, `type`, or `hover` on the "
        "fixed element BEFORE running run_test. A run_test alone does NOT prove the element works "
        "from a user's perspective. Go back and CLICK the fixed element now."
    ),
    cooldown=4,
    priority=10,  # Highest priority — this is a critical verification gap
)
def _verify_without_interact(ctx: _RuleContext) -> bool:
    """Fires when AI does run_test or post_message after inject without clicking first."""
    if ctx.action not in ("run_test", "post_message", "answer_user"):
        return False

    browser_actions = {"click", "scroll", "type", "hover", "click_at_position"}
    fix_actions = {"inject_css", "inject_js"}

    # Walk backwards from current action to find the last fix
    actions = ctx.actions_used
    last_fix_idx = -1
    for i in range(len(actions) - 1, -1, -1):
        if actions[i] in fix_actions:
            last_fix_idx = i
            break

    if last_fix_idx < 0:
        return False  # No fix applied yet

    # Check if there's any browser interaction between the last fix and now
    actions_since_fix = actions[last_fix_idx + 1:]
    has_interaction = any(a in browser_actions for a in actions_since_fix)

    return not has_interaction


# ── Rule: Block layout collapse via body resizing ────────────────────────
@_rule(
    tag="block_body_resizing",
    hint=(
        "⚠️ CRITICAL: Viewport/body width CSS/JS overrides that collapse layouts are prohibited. "
        "Do NOT write CSS targeting body/html width or JS modifying document.body.style.width. "
        "If you need to test responsive styles, use `set_viewport_size` action. "
        "Resizing via CSS/JS breaks layout media queries and causes validation timeouts."
    ),
    cooldown=5,
    priority=10,
)
def _block_body_resizing(ctx: _RuleContext) -> bool:
    """Fires when the agent attempts to modify body or html width in CSS/JS."""
    from engine.validators import check_body_resize_css, check_body_resize_js
    if ctx.action == "inject_css":
        css = ctx.payload.get("css") or ctx.payload.get("code") or ""
        if check_body_resize_css(css):
            return True
    elif ctx.action == "inject_js":
        code = ctx.payload.get("code") or ""
        if check_body_resize_js(code):
            return True
    # Or if the action result contains the rejection error message
    if "[error] Action rejected: Modifying the width" in ctx.result_str:
        return True
    return False


# ── Rule: Select element value assigned plain text instead of option value ─
@_rule(
    tag="select_value_plain_text",
    hint=(
        "⚠️ WARNING: You are setting a select element's `.value` to a plain text string (e.g. 'Dawn'). "
        "In Shopify (and many other systems), the `<option>` value is a Variant ID or GID "
        "(e.g., 'gid://shopify/ProductVariant/44426573807802' or '44426573807802'), not the option's text. "
        "Find the option by its text content first, then select it by setting `.value = option.value` "
        "and dispatching the change/input events. Example:\n"
        "```js\n"
        "const select = document.querySelector('select');\n"
        "const option = Array.from(select.options).find(opt => opt.text.trim().toLowerCase() === selectedColor.toLowerCase());\n"
        "if (option) {\n"
        "  select.value = option.value;\n"
        "  select.dispatchEvent(new Event('change', { bubbles: true }));\n"
        "}\n"
        "```"
    ),
    cooldown=4,
    priority=9,
)
def _select_value_plain_text(ctx: _RuleContext) -> bool:
    """Fires when JS code assigns a plain text literal to a select or option value."""
    if ctx.action != "inject_js":
        return False
    code = ctx.payload.get("code") or ""
    # Look for .value = 'Plain Text' or .value = "Plain Text"
    # Matches letters and spaces, but not GIDs (no slashes, no colons, not pure digits)
    matches = re.findall(r'\.value\s*=\s*[\'"]([a-zA-Z\s_-]{3,20})[\'"]', code)
    if matches:
        return True
    
    # Also search for select/option value setting text
    if "select.value =" in code or "option.value =" in code:
        # If they are assigning variant/color names
        color_keywords = {"dawn", "ice", "powder", "electric", "sunset", "color", "variant", "size"}
        code_lower = code.lower()
        if any(f"value = '{cw}'" in code_lower or f'value = "{cw}"' in code_lower for cw in color_keywords):
            return True
    return False


# ── Rule: Verify selectors before CSS injection ──────────────────────────
@_rule(
    tag="verify_selectors_before_css",
    hint=(
        "⚠️ SELECTOR VALIDATION: Ensure your CSS selectors are accurate! "
        "Before injecting CSS/JS fixes, always query the selector first via `cdp_query_selector_all` "
        "or `run_test` (e.g. `document.querySelector('your-selector')`) to confirm it exists. "
        "Never assume class names from visual inspection alone. For example, a widget might use "
        "`.amp-bundles__volume-discount-bundles__tier-option` rather than a generic `.volume-discount-tier`."
    ),
    cooldown=5,
    priority=8,
)
def _verify_selectors_before_css(ctx: _RuleContext) -> bool:
    """Fires when CSS is injected, to remind the agent to validate selectors."""
    if ctx.action != "inject_css":
        return False
    # If the agent has not used cdp_query_selector_all or inspect_element recently (in the last 4 turns)
    recent = ctx.actions_used[-4:] if len(ctx.actions_used) >= 4 else ctx.actions_used
    has_checked_selector = any(a in ("cdp_query_selector_all", "inspect_element", "run_test") for a in recent)
    return not has_checked_selector


# ── Rule: Update plan warning ──────────────────────────────────────────
@_rule(
    tag="update_plan_warning",
    hint=(
        "⚠️ PLAN WARNING/ERROR: Your `update_plan` call returned a validation warning or error. "
        "Please check your task indexes/IDs carefully! Ensure you are marking the correct task "
        "ID as complete or in-progress, and that you are not completing a fix/verify task "
        "without actually having taken the corresponding actions in this session."
    ),
    cooldown=2,
    priority=10,
)
def _update_plan_warning(ctx: _RuleContext) -> bool:
    """Fires when update_plan returns an error or warning."""
    if ctx.action != "update_plan":
        return False
    rd = ctx.result_dict
    if rd:
        # Check for error/warning keys
        if any(k in rd for k in ("error", "errors", "warnings", "⚠️_warning")):
            return True
    # Fallback to checking result string
    res_str = ctx.result_str.lower()
    if "error" in res_str or "warning" in res_str or "invalid" in res_str or "invalid index" in res_str:
        return True
    return False


# ── Rule: Resilient clicking instead of JS click injection ───────────────
@_rule(
    tag="resilient_clicking",
    hint=(
        "⚠️ RESILIENT CLICKING: Avoid writing programmatic click injections (e.g. `.click()`) in `inject_js` "
        "when a selector-based click is intercepted by overlays or sticky headers. "
        "Instead, retrieve the element's coordinates using `cdp_query_selector_all` or `inspect_element` "
        "and click via the coordinate-based `click_at_position(x, y)` tool. This is more robust and behaves "
        "like a real user interaction."
    ),
    cooldown=4,
    priority=9,
)
def _resilient_clicking(ctx: _RuleContext) -> bool:
    """Fires when JS code contains .click() or similar click injection."""
    if ctx.action != "inject_js":
        return False
    code = ctx.payload.get("code") or ""
    # Look for ".click(" or ".click;" or similar click calls in the JS code
    if ".click(" in code or ".click;" in code or "click()" in code:
        return True
    return False


# ── Rule: CSS specificity wars — repeated inject_css on same target ──────
@_rule(
    tag="css_specificity_wars",
    hint=(
        "⚠️ CSS SPECIFICITY CONFLICT: You've injected CSS multiple times but the element still looks wrong. "
        "Your rules may be getting overridden by higher-specificity theme styles. Options:\n"
        "1. Use `cdp_get_matched_styles` to see ALL CSS rules and their specificity.\n"
        "2. Add `!important` to your CSS declarations.\n"
        "3. Increase specificity with longer selectors (e.g., `body #wrapper .target` instead of `.target`).\n"
        "4. If the element is inside an iframe, CSS injection won't work — use `inject_js` to inject into the iframe document."
    ),
    cooldown=6,
    priority=8,
)
def _css_specificity_wars(ctx: _RuleContext) -> bool:
    """Fires when inject_css used 2+ times and no_changes_after_fix also likely."""
    if ctx.action != "inject_css":
        return False
    # Count inject_css calls in last 8 actions
    recent = ctx.actions_used[-8:] if len(ctx.actions_used) >= 8 else ctx.actions_used
    css_count = sum(1 for a in recent if a == "inject_css")
    return css_count >= 3


# ── Rule: Mobile/responsive testing nudge ────────────────────────────────
@_rule(
    tag="mobile_responsive_nudge",
    hint=(
        "💡 RESPONSIVE CHECK: You've fixed visual issues at desktop width but haven't tested mobile. "
        "Many Shopify themes break differently at mobile widths. Use `set_viewport_size` with "
        "`{\"width\": 375, \"height\": 812}` to test on iPhone-size viewport, then verify your fixes "
        "still work. Switch back to desktop with `{\"width\": 1280, \"height\": 800}` after."
    ),
    cooldown=10,
    priority=5,
)
def _mobile_responsive_nudge(ctx: _RuleContext) -> bool:
    """Fires after verify_fix passes but no set_viewport_size was ever used."""
    if ctx.action != "verify_fix":
        return False
    # Only fire if verify passed
    res_str = ctx.result_str.lower()
    if "fail" in res_str or "error" in res_str:
        return False
    # Check if set_viewport_size was ever used this session
    return "set_viewport_size" not in ctx.actions_used


# ── Rule: Shadow DOM awareness ───────────────────────────────────────────
@_rule(
    tag="shadow_dom_hint",
    hint=(
        "⚠️ SHADOW DOM: Selectors aren't matching but the element exists on the page. "
        "The element may be inside a Shadow DOM root (common with custom Shopify app embeds). "
        "Use `search_all_frames` which also searches shadow roots, or use `inject_js` with "
        "`document.querySelector('host-element').shadowRoot.querySelector('target')` to reach inside."
    ),
    cooldown=5,
    priority=7,
)
def _shadow_dom_hint(ctx: _RuleContext) -> bool:
    """Fires when selector_not_found triggers multiple times — may be shadow DOM."""
    if ctx.action not in ("click", "inspect_element", "cdp_get_computed_style"):
        return False
    res_str = ctx.result_str.lower()
    if "not found" not in res_str and "no element" not in res_str:
        return False
    # Check if this is a repeated failure (3+ selector failures in last 6 actions)
    recent = ctx.actions_used[-6:] if len(ctx.actions_used) >= 6 else ctx.actions_used
    selector_actions = ("click", "inspect_element", "cdp_get_computed_style", "cdp_query_selector_all")
    fail_count = sum(1 for a in recent if a in selector_actions)
    return fail_count >= 3


# ── Rule: Exhaustion fallback — allow post_message after 3+ failed fix attempts ──
@_rule(
    tag="exhaustion_fallback",
    hint=(
        "💡 EXHAUSTION FALLBACK: You've attempted 3+ fixes that didn't fully resolve the issue. "
        "It's OK to `post_message` now with what you've found and attempted. Report:\n"
        "1. What you diagnosed\n"
        "2. What fixes you tried and their results\n"
        "3. What remains unresolved and why\n"
        "This is better than looping endlessly. Use `build_report` to generate a structured report."
    ),
    cooldown=8,
    priority=9,
)
def _exhaustion_fallback(ctx: _RuleContext) -> bool:
    """Fires when 3+ fix cycles have happened without verify_fix passing."""
    if ctx.action not in ("inject_css", "inject_js"):
        return False
    # Count fix attempts (inject_css + inject_js) in the full session
    fix_count = sum(1 for a in ctx.actions_used if a in ("inject_css", "inject_js"))
    # Only fire if we've had 10+ fixes (suggesting 5+ fix-verify cycles) and verify hasn't passed.
    # Previous threshold of 6 was too low for multi-issue audits (6 issues = 12+ expected fixes).
    return fix_count >= 10 and not ctx.verify_fix_done


# ── Rule: Radio button click interception (Shopify pattern) ──────────────
@_rule(
    tag="radio_click_interception",
    hint=(
        "⚠️ RADIO BUTTON CLICK FAILED: Shopify themes hide `<input type=\"radio\">` behind visible `<label>` elements. "
        "The label intercepts pointer events, causing your click to time out. Solutions:\n"
        "1. Use `click_at_position` targeting the LABEL's coordinates (get them via `cdp_query_selector_all` on the label).\n"
        "2. Use `inject_js` to programmatically set the radio: `document.querySelector('input#ID').checked = true; "
        "document.querySelector('input#ID').dispatchEvent(new Event('change', {bubbles: true}));`\n"
        "Do NOT keep retrying `click` on the same radio input — it will always time out."
    ),
    cooldown=5,
    priority=10,
)
def _radio_click_interception(ctx: _RuleContext) -> bool:
    """Fires when a click on an input fails with pointer interception/timeout."""
    if ctx.action != "click":
        return False
    res_str = ctx.result_str.lower()
    # Check for timeout or interception error
    if "timeout" not in res_str and "intercept" not in res_str:
        return False
    # Check if the selector targets a radio input
    selector = (ctx.payload.get("selector") or "").lower()
    if "input" in selector or "radio" in selector:
        return True
    return False


# ── Rule: Repeated click failures suggest coordinate-based clicking ──────
@_rule(
    tag="repeated_click_failure",
    hint=(
        "💡 CLICK KEEPS FAILING: You've had 3+ click failures. The element may be covered by a sticky header, "
        "overlay, or theme wrapper. Use `cdp_query_selector_all` to get the element's bounding rect, then "
        "`click_at_position` with coordinates from the rect's center (x + width/2, y + height/2). "
        "If the element is partially behind a fixed header, scroll down first."
    ),
    cooldown=6,
    priority=9,
)
def _repeated_click_failure(ctx: _RuleContext) -> bool:
    """Fires after 3+ click failures in the last 6 actions."""
    if ctx.action != "click":
        return False
    res_str = ctx.result_str.lower()
    if "timeout" not in res_str and "failed" not in res_str:
        return False
    # Count click failures in recent history
    recent = ctx.actions_used[-6:] if len(ctx.actions_used) >= 6 else ctx.actions_used
    click_count = sum(1 for a in recent if a == "click")
    return click_count >= 3


# ── Rule: CSS hiding failed — switch to JS DOM removal ─────────────────
@_rule(
    tag="css_to_js_fallback",
    hint=(
        "⚠️ CSS HIDING NOT WORKING: You've used inject_css to hide/restyle an element but it's "
        "still visible. Shopify themes often use `!important`, inline styles, or JS that re-applies "
        "styles after your CSS loads. Switch to `inject_js` for reliable DOM manipulation:\n"
        "1. To HIDE: `document.querySelector('#selector').style.setProperty('display','none','important');`\n"
        "2. To REMOVE entirely: `document.querySelector('#selector')?.remove();`\n"
        "3. To RESTYLE: `el.style.setProperty('prop','value','important');` — this beats any CSS specificity.\n"
        "inject_js with `.style.setProperty()` always wins over CSS because it sets inline `!important`."
    ),
    cooldown=5,
    priority=9,
)
def _css_to_js_fallback(ctx: _RuleContext) -> bool:
    """Fires when inject_css was recently used and verify_fix fails or another inject_css follows."""
    # Fire on verify_fix failure after inject_css
    if ctx.action == "verify_fix":
        res_str = ctx.result_str.lower()
        if "fail" in res_str or "not" in res_str or "still" in res_str:
            recent = ctx.actions_used[-5:] if len(ctx.actions_used) >= 5 else ctx.actions_used
            if "inject_css" in recent:
                return True
    # Also fire on repeated inject_css (2nd+ attempt on same issue without verify passing)
    if ctx.action == "inject_css":
        recent = ctx.actions_used[-4:] if len(ctx.actions_used) >= 4 else ctx.actions_used
        css_count = sum(1 for a in recent if a == "inject_css")
        if css_count >= 2:
            return True
    return False


