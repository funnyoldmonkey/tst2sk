"""The agent brain — observe, think, act, repeat. CLI-only."""
import os
import re
import sys
import json
import time
import hashlib
import asyncio
import atexit
import signal
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from config import AppConfig
from browser.controller import BrowserController
from browser.observer import capture_observation
from browser.actions import execute_action
from ai.client import AIClient
from ai.summarizer import SummarizerClient
from ai.prompts import get_system_prompt
from engine.diagnostics import cross_reference_diagnostics
from engine.search import search_dom, search_console, search_network, read_network_body
from engine.kb import append_fix, search_fixes, search_playbook, find_relevant_fixes
from engine.convo_logger import ConvoLogger, copy_fix_to_clipboard, search_conversations, get_conversation_detail
from engine.jit import JITEngine

console = Console()
SCRATCH_DIR = "scratch"
SCRATCH_NET_BODIES = os.path.join(SCRATCH_DIR, "obs_net_bodies")

# Actions that are handled locally (no browser roundtrip)
LOCAL_ACTIONS = {
    "search_dom", "search_console", "search_network",
    "read_network_body", "diagnose", "log_fix",
    "search_playbook", "search_fixes", "search_conversations",
    "get_conversation_detail", "update_plan",
}

# Session commands (case-insensitive)
_CLOSE_KEYWORD = "end"
_SUMMARIZE_KEYWORD = "summarize"


def _has_buffered_input() -> bool:
    """Check if stdin has buffered data (from a paste operation)."""
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.kbhit()
    else:
        import select as _select
        return bool(_select.select([sys.stdin], [], [], 0.02)[0])


def _read_multiline_input(prompt: str = "\n[You] > ") -> str:
    """Read user input with multi-line paste support.

    Phase 1: Drain all buffered stdin (pasted text, including blank lines).
    Phase 2: Show continuation prompt. Blank line = submit, but only when
             nothing is left in the buffer.
    Quick commands ('end', 'copy') submit immediately on a single line.
    """
    first_line = input(prompt)

    # Quick commands — submit immediately, no need for blank line
    stripped = first_line.strip().lower()
    if stripped in (_CLOSE_KEYWORD, _SUMMARIZE_KEYWORD, "copy", ""):
        return first_line.strip()

    lines = [first_line]

    # Phase 1: Drain any buffered input from paste (including blank lines)
    while _has_buffered_input():
        try:
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            lines.append(line.rstrip("\n\r"))
        except Exception:
            break

    # Phase 2: Let user add more or submit with blank line
    while True:
        try:
            line = input("  ... ")
            # Blank line = submit ONLY if buffer is empty (user actually hit Enter)
            if line.strip() == "" and not _has_buffered_input():
                break
            lines.append(line)
        except EOFError:
            break

    return "\n".join(lines).strip()


class Brain:
    """The autonomous troubleshooting agent loop."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.browser = BrowserController(headless=config.headless)
        self.ai = AIClient(config)
        self.summarizer = SummarizerClient(config)
        self.messages: list[dict] = []  # Conversation history for the AI
        self.fix_attempts: list[dict] = []
        self.turn_count = 0
        self.detected_scenario = ""
        self.diagnosis_hints: list[str] = []

        self._last_fix_code = None  # Stores latest fix code for manual copy
        self._fix_copy_offered = False  # Track if copy hint was shown for current fix
        self._summarize_done = False  # Track if user ran 'summarize' command
        self._recent_actions: list[tuple] = []  # Track (action, payload_key) for loop detection
        self._consecutive_errors = 0  # Circuit breaker for non-retryable API errors
        self._consecutive_passive_turns = 0  # Track turns without progress (no click/inject/post_message/navigate)
        self._consecutive_fix_turns = 0  # Track consecutive inject_css/inject_js without verification
        self._prev_obs_stats: dict | None = None  # Previous observation stats for diff tracking
        self._plan: list[dict] = []  # Persistent task plan: [{task, status, findings}]
        self.jit = JITEngine()  # Just-In-Time contextual hint engine
        self.multimodal = config.multimodal  # True = send screenshots, False = text-only
        self.system_prompt = get_system_prompt(self.multimodal)

        # Conversation logger — saves session to convo/ on exit
        self.convo = ConvoLogger()
        self._register_exit_hooks()

    def _register_exit_hooks(self):
        """Register atexit + signal handlers to save conversation on any exit."""
        atexit.register(self._save_convo_sync)
        # Handle Ctrl+C and terminal close gracefully
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._signal_handler)
            except (OSError, ValueError):
                pass  # Some signals can't be caught on all platforms

        # Windows: handle console close (CTRL_CLOSE_EVENT, CTRL_BREAK_EVENT)
        try:
            import platform
            if platform.system() == "Windows":
                import win32api  # type: ignore
                def _win_handler(ctrl_type):
                    self._save_convo_sync()
                    return False  # Let the default handler run after saving
                win32api.SetConsoleCtrlHandler(_win_handler, True)
        except ImportError:
            # win32api not available — fall back to SIGBREAK on Windows
            if hasattr(signal, "SIGBREAK"):
                try:
                    signal.signal(signal.SIGBREAK, self._signal_handler)
                except (OSError, ValueError):
                    pass

    async def _wait_for_hydration(self, timeout: float = 10.0, poll_interval: float = 0.5):
        """Wait for page to be fully hydrated before capturing state.

        Polls until:
        1. document.readyState === 'complete'
        2. At least one interactive element has non-zero dimensions
        3. No major layout shifts in the last poll cycle

        Falls back after timeout — partial page is better than no page.
        """
        import time
        start = time.monotonic()
        last_element_count = 0

        while (time.monotonic() - start) < timeout:
            try:
                state = await self.browser.page.evaluate("""() => {
                    const ready = document.readyState;
                    // Check for visible interactive elements with real dimensions
                    const interactives = document.querySelectorAll('button, a, input, select, [role="button"]');
                    let visibleCount = 0;
                    for (const el of interactives) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) visibleCount++;
                        if (visibleCount >= 3) break;  // Fast exit — 3 visible interactives is enough
                    }
                    return { ready, visibleCount, totalInteractive: interactives.length };
                }""")

                is_complete = state["ready"] == "complete"
                has_visible = state["visibleCount"] >= 3
                element_count = state["totalInteractive"]

                # Page is ready: document complete + visible interactives + DOM has stabilized
                if is_complete and has_visible and element_count == last_element_count:
                    elapsed = round(time.monotonic() - start, 1)
                    await self._log("status", f"Page hydrated ({elapsed}s) — {state['visibleCount']}+ visible interactive elements")
                    return

                last_element_count = element_count

            except Exception:
                pass  # Page might be navigating — keep polling

            await asyncio.sleep(poll_interval)

        # Timeout — log and continue with whatever we have
        elapsed = round(time.monotonic() - start, 1)
        await self._log("status", f"⚠️ Hydration timeout ({elapsed}s) — proceeding with current page state")

    def _signal_handler(self, signum, frame):
        """Save conversation on signal before exiting."""
        self._save_convo_sync()
        raise SystemExit(0)

    def _save_convo_sync(self):
        """Synchronous wrapper to save conversation (for atexit/signal)."""
        filepath = self.convo.save()
        if filepath and not filepath.startswith("["):
            console.print(f"\n[bold cyan]💾 Session saved to: {filepath}[/bold cyan]")

    async def _log(self, msg_type: str, content):
        """Internal logger — prints to CLI with clean formatting."""
        # Always log to conversation logger (full data for session files)
        self.convo.log(msg_type, content)

        if msg_type == "thought":
            console.print(Panel(str(content), title="💭 Thought", style="yellow", expand=False, width=min(console.width, 120)))

        elif msg_type == "action":
            action_icons = {
                "diagnose": "🔍", "search_dom": "🔍", "search_console": "🔍",
                "search_network": "🔍", "search_playbook": "📖", "search_fixes": "📖",
                "search_conversations": "📖", "get_conversation_detail": "📖",
                "inject_css": "🔧", "inject_js": "🔧",
                "click": "👆", "type": "⌨️", "scroll": "📜", "hover": "👆",
                "navigate": "🌐", "observe": "👁️", "run_test": "✅",
                "post_message": "💬", "answer_user": "💬",
                "inspect_element": "🔍", "capture_element": "📸",
                "read_network_body": "📡",
                "clear_site_data": "🗑️", "log_fix": "📝",
                "click_at_position": "👆",
                "cdp_get_dom_tree": "🔬", "cdp_get_cookies": "🍪",
                "cdp_get_computed_style": "🔬", "cdp_get_page_metrics": "📊",
                "cdp_query_selector_all": "🔬",
            }
            icon = action_icons.get(content, "▶️")
            console.print(f"  {icon} [bold]{content}[/bold]")

        elif msg_type == "result":
            self._print_compact_result(content)

        elif msg_type == "agent_message":
            # Unescape literal \n from JSON strings and render markdown
            display_content = str(content).replace("\\n", "\n")
            if "```" in display_content:
                console.print(Panel(Markdown(display_content), title="🤖 Agent Message", style="bold green"))
            else:
                console.print(Panel(display_content, title="🤖 Agent Message", style="bold green"))
            self._offer_copy_fix(content)

        elif msg_type == "status":
            if "Turn" in str(content):
                console.print(f"\n[bold cyan]{content}[/bold cyan]")
            else:
                console.print(f"  [dim]{content}[/dim]")

        elif msg_type == "screenshot":
            console.print(f"  [dim]📸 Screenshot taken[/dim]")

        else:
            console.print(f"  [{msg_type}] {content}")

    def _print_compact_result(self, content):
        """Print a compact, readable summary of action results instead of raw JSON."""
        if isinstance(content, dict):
            if "detected_scenario" in content:
                scenario = content.get("detected_scenario", "unknown")
                conf = content.get("confidence", 0)
                issues = content.get("potential_issues", [])
                console.print(f"  [dim]Scenario: {scenario} ({conf}% confidence)[/dim]")
                for issue in issues[:3]:
                    short = issue[:80] + "..." if len(issue) > 80 else issue
                    console.print(f"  [dim]  • {short}[/dim]")
            elif "total_matches" in content:
                total = content.get("total_matches", 0)
                query = content.get("query", "")
                console.print(f"  [dim]Found {total} matches for \"{query}\"[/dim]")
            elif "results" in content:
                results = content.get("results", [])
                console.print(f"  [dim]Found {len(results)} result(s)[/dim]")
            elif isinstance(content.get("matches"), list):
                matches = content.get("matches", [])
                console.print(f"  [dim]Found {len(matches)} past session(s)[/dim]")
            else:
                keys = list(content.keys())[:5]
                console.print(f"  [dim]Result: {', '.join(keys)}[/dim]")
        elif isinstance(content, str):
            short = content[:120] + "..." if len(content) > 120 else content
            console.print(f"  [dim]{short}[/dim]")
        else:
            console.print(f"  [dim]Done.[/dim]")

    def _extract_code_from_message(self, message: str) -> str | None:
        """Extract the largest code block from a message. Returns code or None."""
        text = str(message).replace("\\n", "\n")
        code_blocks = re.findall(r'```(?:\w+)?\s*\n(.*?)```', text, re.DOTALL)
        if not code_blocks:
            code_blocks = re.findall(r'`([^`]{10,})`', text)
        if code_blocks:
            return max(code_blocks, key=len).strip()
        return None

    def _offer_copy_fix(self, message: str):
        """If message has code or we have a recent fix, show hint to type 'copy'."""
        fix_code = self._extract_code_from_message(message)
        if fix_code:
            self._last_fix_code = fix_code
            self._fix_copy_offered = False

        if self._last_fix_code and not self._fix_copy_offered:
            self._fix_copy_offered = True
            console.print(f"[bold cyan]📋 Fix code available — type 'copy' to copy to clipboard[/bold cyan]")

    @staticmethod
    def _is_close_input(text: str) -> bool:
        """Check if user typed 'end' to close the session. Case-insensitive."""
        return text.strip().lower() == _CLOSE_KEYWORD

    async def start(self, url: str, user_query: str):
        """Main entry point — navigate to URL and start the loop."""
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        os.makedirs(SCRATCH_NET_BODIES, exist_ok=True)

        self.convo.set_session_info(url, user_query)

        mode_label = "🔭 Vision mode (multimodal)" if self.multimodal else "📝 Text-only mode (no screenshots)"
        await self._log("status", f"🚀 Starting TST2SK session\n{mode_label}\n📍 URL: {url}\n❓ Query: {user_query}")

        try:
            await self.browser.launch()
            await self._log("status", "Browser launched")

            await self.browser.navigate(url)
            await self._log("status", f"Navigated to {url}")

            # Wait for page hydration — poll until DOM has visible interactive elements
            await self._wait_for_hydration()

            obs = await capture_observation(self.browser)
            self._write_scratch_files(obs)
            self._persist_network_bodies()

            slim_obs = self._build_slim_observation(obs)
            initial_context = self._build_context_message(slim_obs, user_query, obs["url"])

            self.messages.append({"role": "user", "content": initial_context})

            await self._loop()
        except Exception as e:
            await self._log("status", f"Fatal error during startup: {str(e)}")
            raise e
        finally:
            self._save_convo_sync()

    async def _loop(self):
        """The main observe → think → act → repeat loop."""
        while True:
            self.turn_count += 1
            await self._log("status", f"═══ Turn {self.turn_count} ═══")

            self._trim_history()

            # Capture observation — robust against browser crashes
            try:
                obs = await capture_observation(self.browser)
            except Exception as e:
                await self._log("status", f"⚠️ Observation capture failed: {e}. Retrying...")
                await asyncio.sleep(2)
                try:
                    obs = await capture_observation(self.browser)
                except Exception as e2:
                    await self._log("status", f"❌ Observation capture failed twice: {e2}. Ending session.")
                    break

            if self.multimodal:
                await self._log("screenshot", {"base64": obs["screenshot_base64"], "url": obs["url"]})

            # Get action from AI
            try:
                # Fresh screenshot from this turn's observation
                screenshot_for_ai = obs["screenshot_base64"] if self.multimodal else None

                # Show thinking spinner while waiting for AI
                from rich.live import Live
                from rich.spinner import Spinner
                from rich.text import Text
                spinner = Spinner("dots", text="  [dim]Thinking...[/dim]", style="cyan")
                with Live(spinner, console=console, refresh_per_second=10, transient=True):
                    action_data, full_raw_response = await self.ai.get_action(
                        self.system_prompt, self.messages, screenshot_for_ai
                    )
                await self._log("thought", action_data.get("thought", ""))

                # Reset error counter on success
                self._consecutive_errors = 0

                thought = action_data.get("thought", "")
                action = action_data.get("action", "observe")
                payload = action_data.get("payload", {})

                # ─── Stuck-loop detection (3 layers) ───
                payload_sig = ""
                if action == "observe":
                    payload_sig = ""
                elif action in ("search_dom", "search_console", "search_network",
                                "search_playbook", "search_fixes", "search_conversations"):
                    payload_sig = payload.get("query", "")
                elif action in ("click", "hover", "inspect_element"):
                    payload_sig = payload.get("selector", "")
                elif action in ("inject_css", "inject_js"):
                    payload_sig = payload.get("code", payload.get("css", ""))
                elif action in ("cdp_get_computed_style", "cdp_query_selector_all"):
                    payload_sig = payload.get("selector", "")
                else:
                    payload_sig = str(payload)

                self._recent_actions.append((action, payload_sig))
                if len(self._recent_actions) > 8:
                    self._recent_actions = self._recent_actions[-8:]

                # --- Layer 1: Identical repeat (same action + payload 3x) ---
                loop_detected = False
                if (len(self._recent_actions) >= 3
                    and len(set(self._recent_actions[-3:])) == 1
                    and self._recent_actions[-1][0] != "run_test"):
                    stuck_action = self._recent_actions[-1][0]
                    await self._log("status", f"⚠️ Loop detected — AI repeated '{stuck_action}' with same payload 3 times.")
                    loop_detected = True

                # --- Layer 2: Cycle detection (A→B→C→A→B→C pattern in last 6) ---
                # Compare FULL tuples (action + payload) so run_test with different code
                # doesn't falsely match. Only fall back to action-name-only comparison
                # for actions with empty payload signatures (like observe).
                if not loop_detected and len(self._recent_actions) >= 6:
                    last6 = self._recent_actions[-6:]
                    # Check for 2-step cycle: AB AB AB (full tuple comparison)
                    if last6[0:2] == last6[2:4] == last6[4:6]:
                        cycle_names = [a[0] for a in last6[0:2]]
                        await self._log("status", f"⚠️ Cycle detected — AI repeating {cycle_names} pattern.")
                        loop_detected = True
                    # Check for 3-step cycle: ABC ABC (full tuple comparison)
                    elif last6[0:3] == last6[3:6]:
                        cycle_names = [a[0] for a in last6[0:3]]
                        await self._log("status", f"⚠️ Cycle detected — AI repeating {cycle_names} pattern.")
                        loop_detected = True

                # --- Layer 3: Stagnation (5+ turns of only search/observe/inspect without progress) ---
                progress_actions = {"click", "inject_css", "inject_js", "post_message", "answer_user", "navigate", "type", "click_at_position", "run_test", "update_plan"}
                if action in progress_actions:
                    self._consecutive_passive_turns = 0
                    self._consecutive_fix_turns = 0
                else:
                    self._consecutive_passive_turns += 1

                # Track consecutive fix attempts specifically
                if action in ("inject_css", "inject_js"):
                    self._consecutive_fix_turns += 1
                elif action in ("post_message", "answer_user", "run_test"):
                    self._consecutive_fix_turns = 0

                if not loop_detected and self._consecutive_passive_turns >= 6:
                    await self._log("status", f"⚠️ Stagnation — {self._consecutive_passive_turns} turns of searching/observing without acting.")
                    loop_detected = True

                if not loop_detected and self._consecutive_fix_turns >= 5:
                    await self._log("status", f"⚠️ Fix loop — {self._consecutive_fix_turns} consecutive fix injections without verification.")
                    loop_detected = True

                # --- Nudge on any detection ---
                if loop_detected:
                    self._recent_actions.clear()
                    self._consecutive_passive_turns = 0
                    self._consecutive_fix_turns = 0

                    # Diagnostic reset — clear stale hints so fresh diagnose gives clean data
                    self.detected_scenario = ""
                    self.diagnosis_hints.clear()
                    self._prev_obs_stats = None  # Reset diff tracker for clean comparison

                    # Re-capture fresh observation for the pivot
                    try:
                        fresh_obs = await capture_observation(self.browser)
                        self._write_scratch_files(fresh_obs)
                        self._persist_network_bodies()
                        fresh_slim = self._build_slim_observation(fresh_obs)
                        fresh_diag = cross_reference_diagnostics()
                        self.detected_scenario = fresh_diag.get("detected_scenario", "")
                        self.diagnosis_hints = fresh_diag.get("potential_issues", [])
                        self.convo.set_scenario(self.detected_scenario)

                        diag_summary = fresh_diag.get("summary", "")
                        classified = fresh_diag.get("console_error_classification", {})
                    except Exception:
                        diag_summary = "Diagnostic refresh failed"
                        classified = {}
                        fresh_slim = {}

                    nudge = (
                        "SYSTEM NOTICE: You are stuck in a loop — repeating the same searches or actions without progress. "
                        "STOP what you are doing. Diagnostics have been RESET with fresh data.\n\n"
                        f"Fresh diagnostic: {diag_summary}\n"
                    )
                    if classified.get("suspicious"):
                        nudge += f"⚠️ {classified.get('total_suspicious', 0)} suspicious console errors detected — verify with DOM before trusting.\n"
                    nudge += (
                        "\nYou MUST do ONE of these NOW:\n"
                        "1. Use post_message to report your findings so far to the user and ask for guidance.\n"
                        "2. SKIP this audit item and move on to the NEXT one from the user's checklist.\n"
                        "3. Try a COMPLETELY DIFFERENT approach — if search_dom isn't working, use run_test with JS to query the DOM directly.\n"
                        "Do NOT repeat any search you have already done."
                    )
                    self.messages.append({"role": "assistant", "content": full_raw_response})
                    self.messages.append({"role": "user", "content": nudge})
                    continue

                await self._log("action", action)
                self.messages.append({"role": "assistant", "content": full_raw_response})

            except Exception as e:
                self._consecutive_errors += 1
                await self._log("status", f"AI API error: {e}")

                # Circuit breaker — stop after 3 consecutive non-retryable errors
                if self._consecutive_errors >= 3:
                    await self._log("status", "❌ 3 consecutive API errors. Ending session to prevent infinite loop.")
                    break

                await self._log("status", f"Retrying next turn... ({self._consecutive_errors}/3 consecutive errors)")
                await asyncio.sleep(2)
                continue

            # --- Handle post_message (AI speaking to user) ---
            if action in ("post_message", "answer_user"):
                # ── Completion gate: block post_message if plan tasks remain ──
                if self._plan:
                    incomplete = [t for t in self._plan if t["status"] != "done"]
                    if incomplete:
                        incomplete_names = [t["task"] for t in incomplete[:5]]
                        gate_msg = (
                            f"⚠️ BLOCKED: You have {len(incomplete)} incomplete plan task(s): "
                            f"{incomplete_names}. "
                            "Complete or explicitly skip each task before delivering your report. "
                            "Use `update_plan` with `complete` + `findings` for each."
                        )
                        await self._log("status", gate_msg)
                        self.messages.append({
                            "role": "user",
                            "content": f"System: {gate_msg}\n\nGo back and finish your remaining tasks. "
                                       "If a task is not applicable, mark it complete with findings explaining why."
                        })
                        try:
                            obs = await capture_observation(self.browser)
                        except Exception:
                            obs = {"dom": "", "console": "", "network": "", "screenshot_base64": "", "url": "unknown", "visibility_issues": [], "shopify": None, "interactive_inventory": None}
                        self._write_scratch_files(obs)
                        self._persist_network_bodies()
                        slim_obs = self._build_slim_observation(obs)
                        obs_msg = self._build_observation_message(slim_obs, gate_msg, obs.get("url", "unknown"))
                        self.messages.append({"role": "user", "content": obs_msg})
                        if self.multimodal and obs.get("screenshot_base64"):
                            self.messages.append({
                                "role": "user",
                                "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{obs['screenshot_base64']}"}}],
                            })
                        continue  # Back to top of loop — AI must finish tasks

                self._recent_actions.clear()
                message = payload.get("message", payload.get("text", ""))
                await self._log("agent_message", message)

                # Smarter resolved detection — avoid false positives on negated statements
                msg_lower = message.lower()
                positive_fix_signals = (
                    ("root cause" in msg_lower and "could not" not in msg_lower and "unable" not in msg_lower)
                    or ("verified" in msg_lower and "not verified" not in msg_lower)
                    or ("fix" in msg_lower and "applied" in msg_lower)
                    or ("resolved" in msg_lower and "not resolved" not in msg_lower and "unresolved" not in msg_lower)
                )
                if positive_fix_signals:
                    self.convo.mark_resolved()

                # Wait for user input (supports multi-line paste)
                user_input = _read_multiline_input()

                # Handle 'copy' command
                if user_input.lower() == "copy":
                    if self._last_fix_code:
                        result = copy_fix_to_clipboard(self._last_fix_code, "Fix code")
                        console.print(f"[bold cyan]{result}[/bold cyan]")
                    else:
                        console.print("[dim]No fix code available to copy.[/dim]")
                    user_input = _read_multiline_input()

                # Handle 'summarize' command — AI-powered fix summary + save to KB
                if user_input.lower() == _SUMMARIZE_KEYWORD:
                    await self._summarize_and_save()
                    user_input = _read_multiline_input()

                if self._is_close_input(user_input):
                    # If user skipped summarize, auto-save with fallback entry
                    if self.convo.fixes and not self._summarize_done:
                        try:
                            entry = self._build_fix_entry()
                            result = append_fix(entry)
                            if result.get("success"):
                                await self._log("status", "📝 Fix logged to knowledge base (fallback)")
                        except Exception as e:
                            await self._log("status", f"⚠️ KB logging error: {e}")
                    await self._log("status", "Session complete!")
                    break
                else:
                    self.messages.append({"role": "user", "content": user_input})
                    try:
                        obs = await capture_observation(self.browser)
                    except Exception:
                        obs = {"dom": "", "console": "", "network": "", "screenshot_base64": "", "url": "unknown", "visibility_issues": [], "shopify": None, "interactive_inventory": None}
                    self._write_scratch_files(obs)
                    self._persist_network_bodies()
                    slim_obs = self._build_slim_observation(obs)
                    context = self._build_context_message(slim_obs, user_input, obs["url"])
                    self.messages.append({"role": "user", "content": context})
                    continue

            # --- Handle local actions (no browser needed) ---
            if action in LOCAL_ACTIONS:
                result = self._handle_local_action(action, payload)
                await self._log("result", result)

                # JIT: evaluate for contextual hints
                jit_hints = self.jit.evaluate(
                    action=action, payload=payload, result=result,
                    turn=self.turn_count, scenario=self.detected_scenario,
                )
                result_msg = f"Action result for {action}:\n```json\n{self._safe_json_truncate(result, 3000)}\n```"
                if jit_hints:
                    result_msg += "\n\n" + "\n".join(jit_hints)
                self.messages.append({"role": "user", "content": result_msg})
                continue

            # --- Handle browser actions ---
            if action in ("inject_js", "inject_css"):
                self._record_fix_attempt(action_data)
                fix_code = payload.get("code") or payload.get("css") or ""
                if fix_code:
                    self._last_fix_code = fix_code
                    self._fix_copy_offered = False
                self.convo.record_fix({
                    "turn": self.turn_count,
                    "action": action,
                    "payload": payload,
                    "thought": thought,
                })

            result = await execute_action(self.browser, action, payload)
            await self._log("result", result)

            if action in ("navigate", "click"):
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(1.5)

            try:
                obs = await capture_observation(self.browser)
            except Exception:
                obs = {"dom": "", "console": "", "network": "", "screenshot_base64": "", "url": "unknown", "visibility_issues": [], "shopify": None, "interactive_inventory": None}
            self._write_scratch_files(obs)
            self._persist_network_bodies()
            slim_obs = self._build_slim_observation(obs)

            # JIT: evaluate for contextual hints (with observation context)
            jit_hints = self.jit.evaluate(
                action=action, payload=payload, result=result,
                turn=self.turn_count, scenario=self.detected_scenario,
                slim_obs=slim_obs,
            )

            context = self._build_observation_message(slim_obs, result, obs["url"])
            if jit_hints:
                context += "\n\n" + "\n".join(jit_hints)
            self.messages.append({"role": "user", "content": context})

        # Save conversation at end of loop
        self._save_convo_sync()

    def _handle_local_action(self, action: str, payload: dict) -> dict:
        """Handle actions that don't need the browser."""
        query = payload.get("query", "")

        if action == "diagnose":
            result = cross_reference_diagnostics()
            self.detected_scenario = result.get("detected_scenario", "")
            self.diagnosis_hints = result.get("potential_issues", [])
            self.convo.set_scenario(self.detected_scenario)
            self.convo.set_diagnosis_hints(self.diagnosis_hints)
            return result
        elif action == "search_dom":
            return search_dom(query)
        elif action == "search_console":
            return search_console(query)
        elif action == "search_network":
            return search_network(query)
        elif action == "read_network_body":
            return read_network_body(payload.get("filename", ""))
        elif action == "search_playbook":
            return search_playbook(query)
        elif action == "search_fixes":
            return search_fixes(query)
        elif action == "search_conversations":
            return {"results": search_conversations(query)}
        elif action == "get_conversation_detail":
            filename = payload.get("filename", "")
            detail = get_conversation_detail(filename)
            if detail:
                return {"found": True, "session": detail}
            return {"found": False, "error": f"Session file not found: {filename}"}
        elif action == "log_fix":
            entry = payload.get("entry", "")
            if not entry or len(entry.strip()) < 10:
                entry = self._build_fix_entry()
            return append_fix(entry)
        elif action == "update_plan":
            return self._handle_update_plan(payload)
        return {"error": f"Unknown local action: {action}"}

    def _handle_update_plan(self, payload: dict) -> dict:
        """Handle the update_plan action — create, update, or complete tasks.

        Payload options:
          { "tasks": ["task1", "task2", ...] }           — Set/replace the full task list
          { "complete": 0, "findings": "..." }           — Mark task #0 as done with findings
          { "in_progress": 1 }                           — Mark task #1 as in_progress
          { "add": "new task description" }              — Append a new task
        """
        # Set/replace full plan
        if "tasks" in payload:
            tasks = payload["tasks"]
            if not isinstance(tasks, list) or len(tasks) == 0:
                return {"error": "tasks must be a non-empty list of strings"}
            if len(tasks) > 20:
                tasks = tasks[:20]  # Cap at 20 tasks
            self._plan = [{"task": str(t), "status": "pending", "findings": ""} for t in tasks]
            return {"success": True, "plan": self._format_plan()}

        # Complete a task
        if "complete" in payload:
            idx = payload["complete"]
            if not isinstance(idx, int) or idx < 0 or idx >= len(self._plan):
                return {"error": f"Invalid task index: {idx}. Plan has {len(self._plan)} tasks."}
            self._plan[idx]["status"] = "done"
            if "findings" in payload:
                self._plan[idx]["findings"] = str(payload["findings"])[:500]
            return {"success": True, "plan": self._format_plan()}

        # Mark in_progress
        if "in_progress" in payload:
            idx = payload["in_progress"]
            if not isinstance(idx, int) or idx < 0 or idx >= len(self._plan):
                return {"error": f"Invalid task index: {idx}. Plan has {len(self._plan)} tasks."}
            self._plan[idx]["status"] = "in_progress"
            return {"success": True, "plan": self._format_plan()}

        # Add a task
        if "add" in payload:
            if len(self._plan) >= 20:
                return {"error": "Plan is at max capacity (20 tasks). Complete some first."}
            self._plan.append({"task": str(payload["add"]), "status": "pending", "findings": ""})
            return {"success": True, "plan": self._format_plan()}

        return {"error": "update_plan requires one of: tasks, complete, in_progress, add"}

    def _format_plan(self) -> list[dict]:
        """Format plan for display — compact view with status icons."""
        result = []
        for i, item in enumerate(self._plan):
            status_icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅"}.get(item["status"], "⬜")
            entry = {"id": i, "status": f"{status_icon} {item['status']}", "task": item["task"]}
            if item.get("findings"):
                entry["findings"] = item["findings"]
            result.append(entry)
        return result

    def _get_plan_summary(self) -> dict | None:
        """Get a compact plan summary for injection into observations."""
        if not self._plan:
            return None
        done = sum(1 for t in self._plan if t["status"] == "done")
        total = len(self._plan)
        current = next((t for t in self._plan if t["status"] == "in_progress"), None)
        next_pending = next((t for t in self._plan if t["status"] == "pending"), None)
        summary = {
            "progress": f"{done}/{total} complete",
            "tasks": self._format_plan(),
        }
        if current:
            summary["current_task"] = current["task"]
        elif next_pending:
            summary["next_task"] = next_pending["task"]
        if done == total and total > 0:
            summary["all_done"] = True
            summary["hint"] = "All tasks complete. Compile your findings and use post_message to deliver the report."
        return summary

    @staticmethod
    def _safe_json_truncate(obj: dict, max_chars: int) -> str:
        """Serialize JSON and truncate safely without producing invalid JSON.
        Truncates at the serialized string level, not mid-object."""
        full = json.dumps(obj, indent=2)
        if len(full) <= max_chars:
            return full
        # Truncate and close with a note
        return full[:max_chars] + '\n... [truncated]'

    def _build_context_message(self, slim_obs: dict, query: str, url: str) -> str:
        """Build the initial context message for the AI."""
        relevant = find_relevant_fixes(
            self.detected_scenario, url, self.diagnosis_hints
        )
        context = {
            "query": query,
            "observation": slim_obs,
            "url": url,
        }
        if self.fix_attempts:
            context["previous_fix_attempts"] = {
                "total_attempts": len(self.fix_attempts),
                "attempts": self.fix_attempts,
            }
        if relevant:
            context["relevant_fixes"] = relevant
        plan_summary = self._get_plan_summary()
        if plan_summary:
            context["plan"] = plan_summary

        # Past sessions (convo/) are saved for manual reference but NOT auto-injected.
        # The AI can still use search_conversations / get_conversation_detail if needed.

        context_json = self._safe_json_truncate(context, 12000)
        if self.multimodal:
            return f"Current page state:\n```json\n{context_json}\n```\n\nThe screenshot is attached as an image. LOOK AT IT and describe what you see."
        else:
            return f"Current page state:\n```json\n{context_json}\n```\n\nNo screenshot available (text-only mode). Analyze the DOM, console, and network data. Use search_dom, inspect_element, and run_test to investigate."

    def _build_observation_message(self, slim_obs: dict, action_result: str, url: str) -> str:
        """Build observation message after an action.

        Note: relevant_fixes are NOT re-injected here — they're provided
        in the initial context and on scenario change only, to save context window.
        """
        context = {
            "action_result": action_result,
            "observation": slim_obs,
            "url": url,
        }
        if self.fix_attempts:
            context["previous_fix_attempts"] = {
                "total_attempts": len(self.fix_attempts),
                "attempts": self.fix_attempts[-3:],  # Only last 3 attempts to save context
            }
        plan_summary = self._get_plan_summary()
        if plan_summary:
            context["plan"] = plan_summary

        context_json = self._safe_json_truncate(context, 12000)
        if self.multimodal:
            return f"Observation after action:\n```json\n{context_json}\n```\n\nFresh screenshot attached. LOOK AT IT and describe what changed."
        else:
            return f"Observation after action:\n```json\n{context_json}\n```\n\nNo screenshot (text-only mode). Analyze the updated DOM/console/network data. Use inspect_element or run_test to verify changes."

    def _build_slim_observation(self, obs: dict) -> dict:
        """Build context-friendly slim observation with change tracking."""
        dom_raw = obs.get("dom", "")
        console_raw = obs.get("console", "")
        network_raw = obs.get("network", "")

        dom_lines = dom_raw.split("\n") if dom_raw else []
        interactive = [l for l in dom_lines if l.startswith("★")]
        all_els = [l for l in dom_lines if l.startswith("·") or l.startswith("★")]
        hidden_els = [l for l in dom_lines if "[HIDDEN:" in l]

        # Current stats for diff tracking
        current_stats = {
            "total_elements": len(all_els),
            "interactive_elements": len(interactive),
            "hidden_elements": len(hidden_els),
            "console_errors": 0,
            "network_fails": 0,
            "url": obs.get("url", ""),
        }

        dom_summary = {
            "total_elements": len(all_els),
            "interactive_elements": len(interactive),
            "hidden_elements": len(hidden_els),
            "hint": "Use search_dom(query) to find specific elements. The interactive_inventory below has structured element data you can act on directly.",
        }

        # Attach structured interactive inventory (buttons, links, inputs, forms with selectors)
        # Strip rect from slim view to save context — selectors are enough for the AI to act.
        # Full inventory with rects is in scratch/interactive_inventory.json.
        def _slim_el(el):
            """Strip bounding rect from element dict for context-friendly output."""
            return {k: v for k, v in el.items() if k != "rect"}

        inventory = obs.get("interactive_inventory")
        if inventory:
            # Filter to only visible elements for the main view (hidden ones are in visibility_analysis)
            visible_buttons = [_slim_el(b) for b in inventory.get("buttons", []) if b.get("visible")]
            visible_links = [_slim_el(l) for l in inventory.get("links", []) if l.get("visible")]
            visible_inputs = [_slim_el(i) for i in inventory.get("inputs", []) if i.get("visible", True)]
            dom_summary["interactive_inventory"] = {
                "buttons": visible_buttons[:40],  # Cap to avoid context bloat
                "links": visible_links[:20],
                "inputs": visible_inputs[:15],
                "selects": [_slim_el(s) for s in inventory.get("selects", [])][:10],
                "forms": inventory.get("forms", [])[:10],
                "summary": inventory.get("summary", {}),
            }
        else:
            # Fallback to old preview if inventory capture failed
            dom_summary["preview"] = interactive[:30]

        console_lines = console_raw.split("\n") if console_raw else []
        error_count = sum(1 for l in console_lines if "[error]" in l.lower() or "🚨" in l)
        current_stats["console_errors"] = error_count

        # Lightweight error classification (real vs noise) for every observation
        real_errors = [l for l in console_lines if "[error]" in l.lower() and "warning" not in l.lower()]
        console_summary = {
            "total_lines": len(console_lines),
            "error_count": error_count,
            "real_errors": len(real_errors),
            "hint": "Use search_console(query) to search logs. Run `diagnose` for full error classification.",
            "recent": console_lines[-20:] if console_lines else [],
        }

        network_lines = network_raw.split("\n") if network_raw else []
        fail_count = sum(1 for l in network_lines if "🚨 FAILED" in l)
        current_stats["network_fails"] = fail_count
        network_summary = {
            "total_requests": len(network_lines),
            "fail_count": fail_count,
            "hint": "Use search_network(query) to search activity",
            "recent": network_lines[-20:] if network_lines else [],
        }

        # --- Visibility issues (deep analysis of WHY interactive elements are hidden) ---
        vis_issues = obs.get("visibility_issues", [])
        visibility_summary = None
        if vis_issues:
            visibility_summary = {
                "hidden_interactive_count": len(vis_issues),
                "details": vis_issues[:10],  # Cap at 10 to avoid context bloat
                "hint": "These interactive elements are hidden. The 'causes' field shows WHY — fix the root cause, not the symptom.",
            }

        result = {
            "dom": dom_summary,
            "console": console_summary,
            "network": network_summary,
        }
        if visibility_summary:
            result["visibility_analysis"] = visibility_summary

        # --- Auto-parsed API responses (cart, product, errors) ---
        api_insights = self._parse_api_responses()
        if api_insights:
            result["api_insights"] = api_insights

        # --- Shopify window object data (if captured) ---
        shopify_data = obs.get("shopify")
        if shopify_data:
            result["shopify_context"] = shopify_data

        # --- Observation diff: what changed since last turn ---
        if self._prev_obs_stats is not None:
            changes = []
            prev = self._prev_obs_stats
            d_elements = current_stats["total_elements"] - prev["total_elements"]
            d_interactive = current_stats["interactive_elements"] - prev["interactive_elements"]
            d_hidden = current_stats["hidden_elements"] - prev["hidden_elements"]
            d_errors = current_stats["console_errors"] - prev["console_errors"]
            d_fails = current_stats["network_fails"] - prev["network_fails"]

            if d_elements != 0:
                changes.append(f"DOM elements: {'+' if d_elements > 0 else ''}{d_elements} ({prev['total_elements']}→{current_stats['total_elements']})")
            if d_interactive != 0:
                changes.append(f"Interactive elements: {'+' if d_interactive > 0 else ''}{d_interactive}")
            if d_hidden != 0:
                changes.append(f"Hidden elements: {'+' if d_hidden > 0 else ''}{d_hidden} ({prev['hidden_elements']}→{current_stats['hidden_elements']})")
            if d_errors != 0:
                changes.append(f"Console errors: {'+' if d_errors > 0 else ''}{d_errors}")
            if d_fails != 0:
                changes.append(f"Network failures: {'+' if d_fails > 0 else ''}{d_fails}")
            if current_stats["url"] != prev["url"]:
                changes.append(f"URL changed: {prev['url']} → {current_stats['url']}")

            if changes:
                result["changes_since_last_turn"] = changes
            else:
                result["changes_since_last_turn"] = ["No significant changes detected"]

        self._prev_obs_stats = current_stats
        return result

    def _parse_api_responses(self) -> dict | None:
        """Auto-parse captured network response bodies for actionable data.

        Looks for Shopify cart/product JSON, error responses, and other
        structured API data. Returns a summary dict or None if nothing interesting.
        """
        insights = {}
        try:
            bodies = self.browser.network_bodies
            if not bodies:
                return None

            for url, body in bodies.items():
                url_lower = url.lower()

                # --- Shopify Cart API ---
                if "/cart" in url_lower and (".js" in url_lower or "/cart.json" in url_lower):
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict):
                            cart_info = {}
                            if "items" in data:
                                items = data["items"]
                                cart_info["item_count"] = len(items)
                                cart_info["total_price"] = data.get("total_price", 0)
                                cart_info["currency"] = data.get("currency", "unknown")
                                # Summarize items (first 3)
                                cart_info["items_preview"] = [
                                    {
                                        "title": item.get("title", "?")[:40],
                                        "variant": item.get("variant_title", ""),
                                        "quantity": item.get("quantity", 0),
                                        "available": item.get("available", True),
                                    }
                                    for item in items[:3]
                                ]
                            if "errors" in data:
                                cart_info["errors"] = str(data["errors"])[:200]
                            if cart_info:
                                insights["cart"] = cart_info
                    except (json.JSONDecodeError, KeyError):
                        pass

                # --- Shopify Product API ---
                elif "/products/" in url_lower and ".json" in url_lower:
                    try:
                        data = json.loads(body)
                        product = data.get("product", data)
                        if isinstance(product, dict) and "title" in product:
                            variants = product.get("variants", [])
                            available_variants = [v for v in variants if v.get("available", True)]
                            insights["product"] = {
                                "title": product.get("title", "?")[:50],
                                "total_variants": len(variants),
                                "available_variants": len(available_variants),
                                "unavailable_variants": len(variants) - len(available_variants),
                                "variant_preview": [
                                    {
                                        "title": v.get("title", "?")[:30],
                                        "available": v.get("available", True),
                                        "price": v.get("price", "?"),
                                    }
                                    for v in variants[:5]
                                ],
                            }
                    except (json.JSONDecodeError, KeyError):
                        pass

                # --- Generic API error responses ---
                elif any(k in url_lower for k in ["/api/", ".json"]):
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict):
                            # Check for error patterns
                            has_error = any(k in data for k in ["error", "errors", "message", "status"])
                            if has_error and ("error" in data or "errors" in data):
                                endpoint = url.split("?")[0].split("/")[-1]
                                insights.setdefault("api_errors", []).append({
                                    "endpoint": endpoint[:40],
                                    "error": str(data.get("error") or data.get("errors", ""))[:150],
                                })
                    except (json.JSONDecodeError, KeyError):
                        pass

        except Exception:
            pass  # Don't crash observation if parsing fails

        return insights if insights else None

    def _write_scratch_files(self, obs: dict):
        """Write full observation data to scratch files."""
        try:
            with open(os.path.join(SCRATCH_DIR, "obs_dom.txt"), "w", encoding="utf-8") as f:
                f.write(obs.get("dom", ""))
            with open(os.path.join(SCRATCH_DIR, "obs_console.log"), "w", encoding="utf-8") as f:
                f.write(obs.get("console", ""))
            with open(os.path.join(SCRATCH_DIR, "obs_network.log"), "w", encoding="utf-8") as f:
                f.write(obs.get("network", ""))
            # Write full interactive inventory (with rects) for deep access
            inv = obs.get("interactive_inventory")
            if inv:
                import json as _json
                with open(os.path.join(SCRATCH_DIR, "interactive_inventory.json"), "w", encoding="utf-8") as f:
                    f.write(_json.dumps(inv, indent=1))
        except Exception as e:
            console.print(f"[red]Error writing scratch files: {e}[/red]")

    def _persist_network_bodies(self):
        """Write captured network response bodies to scratch/obs_net_bodies/ for read_network_body."""
        try:
            for url, body in self.browser.network_bodies.items():
                safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', url.split('?')[0].split('/')[-1] or 'index')
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                filename = f"{safe_name}_{url_hash}.txt"
                filepath = os.path.join(SCRATCH_NET_BODIES, filename)
                if not os.path.exists(filepath):
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(f"URL: {url}\n---\n{body}")
        except Exception as e:
            console.print(f"[red]Error persisting network bodies: {e}[/red]")

    def _build_fix_entry(self) -> str:
        """Auto-build a KB fix entry from session data when AI sends empty payload."""
        from datetime import datetime
        from urllib.parse import urlparse

        date_str = datetime.now().strftime("%Y-%m-%d")
        domain = "unknown"
        try:
            domain = urlparse(self.convo.url).netloc if self.convo.url else "unknown"
        except Exception:
            pass
        query = self.convo.query or "unknown"

        fix_lines = []
        for fix in self.convo.fixes:
            action = fix.get("action", "")
            payload = fix.get("payload", {})
            code = payload.get("code") or payload.get("css") or ""
            if code:
                fix_lines.append(f"  [{action}] {code.strip()}")

        last_thought = ""
        for fix in reversed(self.convo.fixes):
            t = fix.get("thought", "")
            if t:
                last_thought = t[:200]
                break

        code_block = "\n".join(fix_lines) if fix_lines else "No code recorded"

        entry = f"""---
[{date_str}] store: {domain}
Symptom: {query}
Root Cause: {last_thought}
Fix ({len(self.convo.fixes)} injection(s)):
{code_block}
Verified: {'Yes' if self.convo.resolved else 'No'}"""
        return entry

    async def _summarize_and_save(self):
        """Run subagent to summarize fixes, display to user, save to KB."""
        if not self.convo.fixes:
            console.print("[dim]No fixes to summarize.[/dim]")
            return

        # Try AI-powered summary
        entry = await self.summarizer.build_fix_entry(
            url=self.convo.url or "",
            query=self.convo.query or "",
            fixes=self.convo.fixes,
            scenario=self.detected_scenario or "",
            resolved=self.convo.resolved,
        )

        # Fallback to deterministic builder
        if not entry:
            entry = self._build_fix_entry()
            console.print("[dim]Using deterministic summary (AI unavailable)[/dim]")

        # Display the summary as Agent Message
        await self._log("agent_message", f"📋 Fix Summary:\n\n{entry}")

        # Save to KB
        try:
            result = append_fix(entry)
            if result.get("success"):
                await self._log("status", "📝 Fix saved to knowledge base")
            else:
                await self._log("status", f"⚠️ Could not save fix: {result.get('error', 'unknown')}")
        except Exception as e:
            await self._log("status", f"⚠️ KB save error: {e}")

        self._summarize_done = True

    def _record_fix_attempt(self, action_data: dict):
        """Track fix attempts for the AI to avoid repeating mistakes."""
        self.fix_attempts.append({
            "turn": self.turn_count,
            "action": action_data.get("action"),
            "payload": action_data.get("payload"),
            "thought": action_data.get("thought"),
        })

    def _trim_history(self):
        """Trim conversation history to keep within model limits.
        Always preserves the first message (original query + page context)."""
        if len(self.messages) > self.config.max_history:
            # Keep first message (original context) + most recent messages
            first_msg = self.messages[0]
            recent = self.messages[-(self.config.max_history - 1):]
            self.messages = [first_msg] + recent
