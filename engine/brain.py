"""The agent brain — observe, think, act, repeat."""
import os
import re
import json
import time
import hashlib
import asyncio
import atexit
import signal
from pathlib import Path
from typing import Callable, Awaitable, Optional
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from config import AppConfig
from browser.controller import BrowserController
from browser.observer import capture_observation
from browser.actions import execute_action
from ai.client import AIClient
from ai.prompts import get_system_prompt
from engine.diagnostics import cross_reference_diagnostics
from engine.search import search_dom, search_console, search_network, read_network_body
from engine.kb import append_fix, search_fixes, search_playbook, find_relevant_fixes
from engine.convo_logger import ConvoLogger, copy_fix_to_clipboard, search_conversations, get_conversation_detail

console = Console()
SCRATCH_DIR = "scratch"
SCRATCH_NET_BODIES = os.path.join(SCRATCH_DIR, "obs_net_bodies")

# Actions that are handled locally (no browser roundtrip)
LOCAL_ACTIONS = {
    "search_dom", "search_console", "search_network",
    "read_network_body", "diagnose", "log_fix",
    "search_playbook", "search_fixes", "search_conversations",
    "get_conversation_detail",
}

class Brain:
    """The autonomous troubleshooting agent loop."""

    def __init__(self, config: AppConfig,
                 output_callback: Optional[Callable[[str, any], Awaitable[None]]] = None,
                 input_queue: Optional[asyncio.Queue] = None):
        self.config = config
        self.browser = BrowserController(headless=config.headless)
        self.ai = AIClient(config)
        self.messages: list[dict] = []  # Conversation history for the AI
        self.fix_attempts: list[dict] = []
        self.turn_count = 0
        self.detected_scenario = ""
        self.diagnosis_hints: list[str] = []

        self.output_callback = output_callback
        self.input_queue = input_queue
        self._last_fix_code = None  # Stores latest fix code for manual copy
        self._fix_copy_offered = False  # Track if copy hint was shown for current fix
        self._recent_actions: list[tuple] = []  # Track (action, payload_key) for loop detection
        self.multimodal = config.multimodal  # True = send screenshots, False = text-only
        self.system_prompt = get_system_prompt(self.multimodal)

        # Wire up retry callback for Web UI mode only
        # CLI mode: client handles countdown display directly with Rich Live
        if self.output_callback:
            self.ai.on_retry = self._on_retry

        # Conversation logger — saves session to convo/ on exit
        self.convo = ConvoLogger()
        self._register_exit_hooks()

    async def _on_retry(self, message: str):
        """Callback from AIClient during retry countdown."""
        await self._log("retry", message)

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

    def _signal_handler(self, signum, frame):
        """Save conversation on signal before exiting."""
        self._save_convo_sync()
        raise SystemExit(0)

    def _save_convo_sync(self):
        """Synchronous wrapper to save conversation (for atexit/signal)."""
        filepath = self.convo.save()
        if filepath and not filepath.startswith("["):
            console.print(f"\n[bold cyan]💾 Session saved to: {filepath}[/bold cyan]")

    async def _log(self, msg_type: str, content: any):
        """Internal logger that uses the callback if available, else prints to console.

        CLI output is kept clean and conversational:
        - Thought panel (what the AI is thinking)
        - Action one-liner (what it's doing)
        - Compact result summary (not raw JSON dumps)
        - Agent message (when AI speaks to user)
        - Retry countdown (live countdown on API errors)
        """
        # Always log to conversation logger (full data for session files)
        self.convo.log(msg_type, content)

        if self.output_callback:
            await self.output_callback(msg_type, content)
        else:
            # ─── Clean CLI output ───
            if msg_type == "thought":
                console.print(Panel(str(content), title="💭 Thought", style="yellow", expand=False, width=min(console.width, 120)))

            elif msg_type == "action":
                # Clean action display with context-aware icons
                action_icons = {
                    "diagnose": "🔍", "search_dom": "🔍", "search_console": "🔍",
                    "search_network": "🔍", "search_playbook": "📖", "search_fixes": "📖",
                    "search_conversations": "📖", "get_conversation_detail": "📖",
                    "inject_css": "🔧", "inject_js": "🔧",
                    "click": "👆", "type": "⌨️", "scroll": "📜", "hover": "👆",
                    "navigate": "🌐", "observe": "👁️", "run_test": "✅",
                    "post_message": "💬", "answer_user": "💬",
                    "inspect_element": "🔍", "capture_element": "📸",
                    "read_network_body": "📡", "get_network_body": "📡",
                    "clear_site_data": "🗑️", "log_fix": "📝",
                    "click_at_position": "👆",
                }
                icon = action_icons.get(content, "▶️")
                console.print(f"  {icon} [bold]{content}[/bold]")

            elif msg_type == "result":
                # Compact result summary — no raw JSON dumps
                self._print_compact_result(content)

            elif msg_type == "agent_message":
                # Unescape literal \n from JSON strings and render markdown
                display_content = str(content).replace("\\n", "\n")
                if "```" in display_content:
                    # Has code blocks — use Rich Markdown for proper rendering
                    console.print(Panel(Markdown(display_content), title="🤖 Agent Message", style="bold green"))
                else:
                    console.print(Panel(display_content, title="🤖 Agent Message", style="bold green"))
                # Check if there's a code block — offer manual copy instead of auto-copying
                self._offer_copy_fix(content)

            elif msg_type == "status":
                # Turn headers get special formatting
                if "Turn" in str(content):
                    console.print(f"\n[bold cyan]{content}[/bold cyan]")
                else:
                    console.print(f"  [dim]{content}[/dim]")

            elif msg_type == "screenshot":
                console.print(f"  [dim]📸 Screenshot taken[/dim]")

            elif msg_type == "retry":
                # Only reached in Web UI mode (CLI uses Rich Live in client directly)
                console.print(f"  [dim]{content}[/dim]")

            elif msg_type == "thought_chunk":
                pass  # Streaming — skip in CLI (full thought printed after)

            else:
                console.print(f"  [{msg_type}] {content}")

    def _print_compact_result(self, content):
        """Print a compact, readable summary of action results instead of raw JSON."""
        if isinstance(content, dict):
            # Diagnose results
            if "detected_scenario" in content:
                scenario = content.get("detected_scenario", "unknown")
                conf = content.get("confidence", 0)
                issues = content.get("potential_issues", [])
                console.print(f"  [dim]Scenario: {scenario} ({conf}% confidence)[/dim]")
                for issue in issues[:3]:
                    # Shorten the issue text
                    short = issue[:80] + "..." if len(issue) > 80 else issue
                    console.print(f"  [dim]  • {short}[/dim]")

            # DOM/console/network search results
            elif "total_matches" in content:
                total = content.get("total_matches", 0)
                query = content.get("query", "")
                console.print(f"  [dim]Found {total} matches for \"{query}\"[/dim]")

            # Playbook/fixes search results
            elif "results" in content:
                results = content.get("results", [])
                console.print(f"  [dim]Found {len(results)} result(s)[/dim]")

            # Conversation search results
            elif isinstance(content.get("matches"), list):
                matches = content.get("matches", [])
                console.print(f"  [dim]Found {len(matches)} past session(s)[/dim]")

            # Generic dict — show keys only
            else:
                keys = list(content.keys())[:5]
                console.print(f"  [dim]Result: {', '.join(keys)}[/dim]")

        elif isinstance(content, str):
            # String result — truncate
            short = content[:120] + "..." if len(content) > 120 else content
            console.print(f"  [dim]{short}[/dim]")
        else:
            console.print(f"  [dim]Done.[/dim]")

    def _extract_code_from_message(self, message: str) -> str | None:
        """Extract the largest code block from a message. Returns code or None."""
        # Unescape literal \n from JSON before regex matching
        text = str(message).replace("\\n", "\n")
        # Try fenced code blocks first (```code```)
        code_blocks = re.findall(r'```(?:\w+)?\s*\n(.*?)```', text, re.DOTALL)
        if not code_blocks:
            # Try inline backtick blocks (`code`)
            code_blocks = re.findall(r'`([^`]{10,})`', text)
        if code_blocks:
            return max(code_blocks, key=len).strip()
        return None

    def _offer_copy_fix(self, message: str):
        """If message has code or we have a recent fix, show hint to type 'copy'."""
        # First try extracting from the message itself
        fix_code = self._extract_code_from_message(message)
        if fix_code:
            self._last_fix_code = fix_code
            self._fix_copy_offered = False  # New code found — reset the flag

        # Show hint only once per fix code
        if self._last_fix_code and not self._fix_copy_offered:
            self._fix_copy_offered = True
            console.print(f"[bold cyan]📋 Fix code available — type 'copy' to copy to clipboard[/bold cyan]")

    async def start(self, url: str, user_query: str):
        """Main entry point — navigate to URL and start the loop."""
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        os.makedirs(SCRATCH_NET_BODIES, exist_ok=True)

        # Initialize conversation logger with session info
        self.convo.set_session_info(url, user_query)

        mode_label = "🔭 Vision mode (multimodal)" if self.multimodal else "📝 Text-only mode (no screenshots)"
        await self._log("status", f"🚀 Starting TST2SK session\n{mode_label}\n📍 URL: {url}\n❓ Query: {user_query}")

        try:
            await self.browser.launch()
            await self._log("status", "Browser launched")

            await self.browser.navigate(url)
            await self._log("status", f"Navigated to {url}")

            # Wait a moment for page to settle
            await asyncio.sleep(2)

            # Capture initial observation
            obs = await capture_observation(self.browser)
            self._write_scratch_files(obs)
            self._persist_network_bodies()

            # Build initial context message for the AI
            slim_obs = self._build_slim_observation(obs)
            initial_context = self._build_context_message(slim_obs, user_query, obs["url"])

            self.messages.append({"role": "user", "content": initial_context})

            # Start the agent loop
            await self._loop(obs["screenshot_base64"])
        except Exception as e:
            await self._log("status", f"Fatal error during startup: {str(e)}")
            raise e
        finally:
            # Always save conversation regardless of how session ends
            self._save_convo_sync()

    async def _loop(self, current_screenshot: str):
        """The main observe → think → act → repeat loop."""
        while True:
            self.turn_count += 1
            await self._log("status", f"═══ Turn {self.turn_count} ═══")

            # Trim history to prevent context overflow
            self._trim_history()

            # Capture observation (always — we need DOM/console/network regardless)
            obs = await capture_observation(self.browser)
            # Only show/send screenshot in multimodal mode
            if self.multimodal:
                await self._log("screenshot", {"base64": obs["screenshot_base64"], "url": obs["url"]})

            # Get action from AI
            try:
                full_raw_response = ""
                action_data = None

                # In text-only mode, don't send the screenshot to the AI
                screenshot_for_ai = current_screenshot if self.multimodal else None

                if self.output_callback:
                    # Streaming mode for Web UI
                    async for chunk in self.ai.stream_get_action(
                        self.system_prompt, self.messages, screenshot_for_ai
                    ):
                        full_raw_response += chunk
                        await self._log("thought_chunk", chunk)

                    # Parse the final result (reuse the smart parser from AIClient)
                    action_data = AIClient._parse_json_response(full_raw_response)
                else:
                    # CLI mode: show thinking spinner while waiting for AI
                    from rich.live import Live
                    from rich.spinner import Spinner
                    from rich.text import Text
                    spinner = Spinner("dots", text="  [dim]Thinking...[/dim]", style="cyan")
                    with Live(spinner, console=console, refresh_per_second=10, transient=True) as live:
                        action_data, raw_response = await self.ai.get_action(
                            self.system_prompt, self.messages, screenshot_for_ai
                        )
                    full_raw_response = raw_response
                    await self._log("thought", action_data.get("thought", ""))

                thought = action_data.get("thought", "")
                action = action_data.get("action", "observe")
                payload = action_data.get("payload", {})

                # ─── Stuck-loop detection ───
                # Tracks (action, payload_signature) to distinguish productive
                # investigation (same action, different queries) from stuck loops
                # (same action, same or empty payload repeated).
                payload_sig = ""
                if action == "observe":
                    payload_sig = ""  # observe always has empty payload
                elif action in ("search_dom", "search_console", "search_network",
                                "search_playbook", "search_fixes", "search_conversations"):
                    payload_sig = payload.get("query", "")
                elif action in ("click", "hover", "inspect_element"):
                    payload_sig = payload.get("selector", "")
                else:
                    payload_sig = str(payload)[:100]

                self._recent_actions.append((action, payload_sig))
                if len(self._recent_actions) > 5:
                    self._recent_actions = self._recent_actions[-5:]

                # Check last 3: same action AND same payload = stuck
                if (len(self._recent_actions) >= 3
                    and len(set(self._recent_actions[-3:])) == 1
                    and self._recent_actions[-1][0] not in ("inject_css", "inject_js", "run_test")):
                    stuck_action = self._recent_actions[-1][0]
                    await self._log("status", f"⚠️ Loop detected — AI repeated '{stuck_action}' with same payload 3 times. Nudging to respond.")
                    self._recent_actions.clear()
                    nudge = (
                        "SYSTEM NOTICE: You have repeated the same action 3 times without progress. "
                        "You MUST use post_message NOW to communicate with the user. "
                        "Tell them your findings, the fix you applied, and ask if everything looks good."
                    )
                    self.messages.append({"role": "assistant", "content": full_raw_response})
                    self.messages.append({"role": "user", "content": nudge})
                    continue

                await self._log("action", action)
                self.messages.append({"role": "assistant", "content": full_raw_response})

            except Exception as e:
                await self._log("status", f"AI API error after retries: {e}")
                await self._log("status", "All retry attempts exhausted for this turn. Moving to next turn...")
                await asyncio.sleep(2)
                continue

            # --- Handle post_message (AI speaking to user) ---
            if action in ("post_message", "answer_user"):
                self._recent_actions.clear()  # Reset loop detection — AI is communicating
                message = payload.get("message", payload.get("text", ""))
                await self._log("agent_message", message)

                # If fix was delivered, update convo logger
                if any(kw in message.lower() for kw in ["root cause", "fix", "verified", "resolved"]):
                    self.convo.mark_resolved()

                # Wait for user input
                if self.input_queue:
                    user_input = await self.input_queue.get()
                else:
                    user_input = input("\n[You] > ").strip()

                # Handle 'copy' command — copy last fix code to clipboard
                if user_input.lower() == "copy":
                    if self._last_fix_code:
                        result = copy_fix_to_clipboard(self._last_fix_code, "Fix code")
                        console.print(f"[bold cyan]{result}[/bold cyan]")
                    else:
                        console.print("[dim]No fix code available to copy.[/dim]")
                    # Re-prompt — don't send 'copy' to the AI
                    if self.input_queue:
                        user_input = await self.input_queue.get()
                    else:
                        user_input = input("\n[You] > ").strip()

                if user_input.lower() in ("yes", "looks good", "all good", "done", "close"):
                    await self._log("status", "Session complete!")
                    break
                else:
                    self.messages.append({"role": "user", "content": user_input})
                    obs = await capture_observation(self.browser)
                    self._write_scratch_files(obs)
                    self._persist_network_bodies()
                    slim_obs = self._build_slim_observation(obs)
                    context = self._build_context_message(slim_obs, user_input, obs["url"])
                    self.messages.append({"role": "user", "content": context})
                    current_screenshot = obs["screenshot_base64"]
                    continue

            # --- Handle local actions (no browser needed) ---
            if action in LOCAL_ACTIONS:
                result = self._handle_local_action(action, payload)
                await self._log("result", result)

                result_msg = f"Action result for {action}:\n```json\n{json.dumps(result, indent=2)[:3000]}\n```"
                self.messages.append({"role": "user", "content": result_msg})
                continue

            # --- Handle browser actions ---
            if action in ("inject_js", "inject_css"):
                self._record_fix_attempt(action_data)
                # Store the fix code for manual clipboard copy
                fix_code = payload.get("code") or payload.get("css") or ""
                if fix_code:
                    self._last_fix_code = fix_code
                    self._fix_copy_offered = False  # New fix — allow hint to show again
                # Also log fix to convo logger
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

            obs = await capture_observation(self.browser)
            self._write_scratch_files(obs)
            self._persist_network_bodies()
            slim_obs = self._build_slim_observation(obs)

            context = self._build_observation_message(slim_obs, result, obs["url"])
            self.messages.append({"role": "user", "content": context})
            current_screenshot = obs["screenshot_base64"]


        # Save conversation at end of loop
        self._save_convo_sync()

    def _handle_local_action(self, action: str, payload: dict) -> dict:
        """Handle actions that don't need the browser."""
        query = payload.get("query", "")

        if action == "diagnose":
            result = cross_reference_diagnostics()
            self.detected_scenario = result.get("detected_scenario", "")
            self.diagnosis_hints = result.get("potential_issues", [])
            # Update convo logger with scenario info
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
            # If AI sent empty/short entry, auto-build from session data
            if not entry or len(entry.strip()) < 10:
                entry = self._build_fix_entry()
            return append_fix(entry)
        return {"error": f"Unknown local action: {action}"}

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

        # Auto-search past conversations for relevant context
        past_sessions = search_conversations(query)
        if not past_sessions and url:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            if domain:
                past_sessions = search_conversations(domain)
        if past_sessions:
            context["past_sessions"] = past_sessions

        if self.multimodal:
            return f"Current page state:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nThe screenshot is attached as an image. LOOK AT IT and describe what you see."
        else:
            return f"Current page state:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nNo screenshot available (text-only mode). Analyze the DOM, console, and network data. Use search_dom, inspect_element, and run_test to investigate."

    def _build_observation_message(self, slim_obs: dict, action_result: str, url: str) -> str:
        """Build observation message after an action."""
        relevant = find_relevant_fixes(
            self.detected_scenario, url, self.diagnosis_hints
        )
        context = {
            "action_result": action_result,
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
        if self.multimodal:
            return f"Observation after action:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nFresh screenshot attached. LOOK AT IT and describe what changed."
        else:
            return f"Observation after action:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nNo screenshot (text-only mode). Analyze the updated DOM/console/network data. Use inspect_element or run_test to verify changes."

    def _build_slim_observation(self, obs: dict) -> dict:
        """Build context-friendly slim observation."""
        dom_raw = obs.get("dom", "")
        console_raw = obs.get("console", "")
        network_raw = obs.get("network", "")

        dom_lines = dom_raw.split("\n") if dom_raw else []
        interactive = [l for l in dom_lines if l.startswith("★")]
        all_els = [l for l in dom_lines if l.startswith("·") or l.startswith("★")]

        dom_summary = {
            "total_elements": len(all_els),
            "interactive_elements": len(interactive),
            "hint": "Use search_dom(query) to find specific elements",
            "preview": interactive[:30],
        }

        console_lines = console_raw.split("\n") if console_raw else []
        error_count = sum(1 for l in console_lines if "[error]" in l.lower() or "🚨" in l)
        console_summary = {
            "total_lines": len(console_lines),
            "error_count": error_count,
            "hint": "Use search_console(query) to search logs",
            "recent": console_lines[-20:] if console_lines else [],
        }

        network_lines = network_raw.split("\n") if network_raw else []
        fail_count = sum(1 for l in network_lines if "🚨 FAILED" in l)
        network_summary = {
            "total_requests": len(network_lines),
            "fail_count": fail_count,
            "hint": "Use search_network(query) to search activity",
            "recent": network_lines[-20:] if network_lines else [],
        }

        return {
            "dom": dom_summary,
            "console": console_summary,
            "network": network_summary,
        }

    def _write_scratch_files(self, obs: dict):
        """Write full observation data to scratch files."""
        try:
            with open(os.path.join(SCRATCH_DIR, "obs_dom.txt"), "w", encoding="utf-8") as f:
                f.write(obs.get("dom", ""))
            with open(os.path.join(SCRATCH_DIR, "obs_console.log"), "w", encoding="utf-8") as f:
                f.write(obs.get("console", ""))
            with open(os.path.join(SCRATCH_DIR, "obs_network.log"), "w", encoding="utf-8") as f:
                f.write(obs.get("network", ""))
        except Exception as e:
            console.print(f"[red]Error writing scratch files: {e}[/red]")

    def _persist_network_bodies(self):
        """Write captured network response bodies to scratch/obs_net_bodies/ for read_network_body."""
        try:
            for url, body in self.browser.network_bodies.items():
                # Create a safe filename from the URL
                safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', url.split('?')[0].split('/')[-1] or 'index')
                # Add a hash suffix to avoid collisions
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
        domain = urlparse(self.convo.url).netloc if self.convo.url else "unknown"
        query = self.convo.query or "unknown"

        # Gather fix details from recorded fixes
        fix_lines = []
        for fix in self.convo.fixes:
            action = fix.get("action", "")
            payload = fix.get("payload", {})
            code = payload.get("code") or payload.get("css") or ""
            thought = fix.get("thought", "")
            if code:
                fix_lines.append(f"  [{action}] {code.strip()}")

        # Build the last thought for root cause context
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

    def _record_fix_attempt(self, action_data: dict):
        """Track fix attempts for the AI to avoid repeating mistakes."""
        self.fix_attempts.append({
            "turn": self.turn_count,
            "action": action_data.get("action"),
            "payload": action_data.get("payload"),
            "thought": action_data.get("thought"),
        })

    def _trim_history(self):
        """Trim conversation history to keep within model limits."""
        if len(self.messages) > self.config.max_history:
            self.messages = self.messages[-self.config.max_history:]
