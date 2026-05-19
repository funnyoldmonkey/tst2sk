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
        self._cdp_used: bool = False

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
        elif action == "search_playbook":
            self._playbook_searched = True
        elif action == "search_fixes":
            self._fixes_searched = True
        elif action == "search_conversations":
            self._conversations_searched = True
        elif action.startswith("cdp_"):
            self._cdp_used = True

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
            cdp_used=self._cdp_used,
        )

        # Evaluate all rules (each rule is isolated — one bad rule can't kill the engine)
        fired = []
        for rule, check_fn in _ALL_RULES:
            # Cooldown check
            last = self._last_fired.get(rule.tag, -999)
            if (turn - last) < rule.cooldown:
                continue
            # Trigger check — isolated so one broken rule doesn't kill all JIT
            try:
                if check_fn(ctx):
                    fired.append((rule.priority, rule.hint, rule.tag))
                    self._last_fired[rule.tag] = turn
            except Exception:
                pass  # Skip broken rule silently

        # Sort by priority (highest first)
        fired.sort(key=lambda x: -x[0])

        # Cap at 3 hints per turn to avoid overwhelming the AI
        return [hint for _, hint, _ in fired[:3]]


class _RuleContext:
    """Context object passed to rule trigger functions."""
    __slots__ = (
        "action", "payload", "result", "turn", "scenario", "slim_obs",
        "actions_used", "search_cache", "diagnose_done",
        "playbook_searched", "fixes_searched", "conversations_searched",
        "cdp_used",
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
    return inspect_count >= 2 and not ctx.cdp_used


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
    return not ctx.cdp_used and ctx.turn >= 3


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
    return total >= 900 and not ctx.cdp_used


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
    return search_dom_count >= 3 and not ctx.cdp_used


# ─── Missing Tool Usage ──────────────────────────────────────────

@_rule(
    tag="missing_diagnose",
    hint=(
        "JIT HINT: You haven't run `diagnose` yet. It provides scenario detection, "
        "console error classification (real vs suspicious), Shopify analysis, and "
        "hidden element counts. Run it FIRST — it gives you the full picture."
    ),
    cooldown=10,
    priority=10,
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
    return "inject_css" not in ctx.actions_used


@_rule(
    tag="fix_without_verify",
    hint=(
        "JIT HINT: You applied a fix but haven't verified it yet. MANDATORY after every fix: "
        "1) Run `run_test` with getComputedStyle assertions, "
        "2) Check the screenshot/DOM for visual confirmation. "
        "Both must pass before declaring the fix working."
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
        "JIT HINT: Changes were detected after your action! DOM/console/network shifted. "
        "Run `run_test` NOW to verify if the change is what you intended. "
        "Check computed styles on the target element to confirm the fix took effect."
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

    # Check DOM summary for overlay-related content
    dom_summary = str(slim.get("dom_summary", "")).lower()
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
