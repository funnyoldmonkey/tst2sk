"""OpenAI-compatible AI client with vision support + retry logic for free-tier APIs."""
import json
import asyncio
import logging
from openai import AsyncOpenAI
from rich.console import Console
from rich.text import Text
from config import AppConfig

_console = Console()

logger = logging.getLogger("tst2sk.ai")

# Retry config — incremental backoff (3s, 6s, 9s, ...) up to 10 attempts
MAX_RETRIES = 10
BASE_DELAY = 3  # seconds — each retry adds this: 3, 6, 9, 12, ...


class AIClient:
    """Sends observations + screenshot to AI model, receives action back."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            default_headers=config.extra_headers if config.extra_headers else None,
        )
        self.model = config.model

    async def _countdown(self, delay: int, attempt: int, error_msg: str):
        """Count down with live updates using Rich Live display."""
        short_err = str(error_msg).split(" - ")[0][:80] if error_msg else "Unknown error"

        from rich.live import Live
        _console.print(f"  [yellow]⚠️  API error (attempt {attempt}/{MAX_RETRIES}): {short_err}[/yellow]")
        with Live(Text(f"  ⏳ Retrying in {delay}s...", style="dim"), console=_console, refresh_per_second=2) as live:
            for remaining in range(delay, 0, -1):
                live.update(Text(f"  ⏳ Retrying in {remaining}s...", style="dim"))
                await asyncio.sleep(1)
            live.update(Text(f"  🔄 Retry {attempt + 1}/{MAX_RETRIES}...", style="bold cyan"))

    def _build_api_messages(
        self,
        system_prompt: str,
        messages: list[dict],
        screenshot_base64: str | None = None,
    ) -> list[dict]:
        """Build the messages array for the API call."""
        api_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            api_messages.append(msg)

        if screenshot_base64 and api_messages:
            last_user_idx = None
            for i in range(len(api_messages) - 1, -1, -1):
                if api_messages[i]["role"] == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                text_content = api_messages[last_user_idx]["content"]
                api_messages[last_user_idx]["content"] = [
                    {"type": "text", "text": text_content},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_base64}",
                            "detail": "high",
                        },
                    },
                ]
        return api_messages

    @staticmethod
    def _is_retryable(e: Exception) -> bool:
        """Check if an exception is retryable (rate limit, server error, timeout)."""
        error_str = str(e).lower()
        status = getattr(e, "status_code", None) or getattr(e, "code", None)

        return (
            status in (429, 500, 502, 503, 529)
            or isinstance(e, (asyncio.TimeoutError, TimeoutError))
            or "rate" in error_str
            or "overloaded" in error_str
            or "500" in error_str
            or "503" in error_str
            or "429" in error_str
            or "quota" in error_str
            or "capacity" in error_str
            or "timeout" in error_str
            or "timed out" in error_str
        )

    @staticmethod
    def _parse_json_response(raw_text: str) -> dict:
        """Extract JSON action from raw AI response text.

        Tries multiple strategies:
        1. Standard fenced JSON block (```json ... ```)
        2. Any fenced block (``` ... ```)
        3. First { ... } in the raw text
        4. Scan for any JSON object with "action" key
        5. Regex extraction of action/thought/payload from garbled text
        """
        import re

        # Strategy 1-3: Extract JSON from common wrappers
        json_text = raw_text
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0].strip()

        start = json_text.find("{")
        end = json_text.rfind("}") + 1
        if start != -1 and end > start:
            json_text = json_text[start:end]

        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            pass

        # Strategy 4: Try to find ANY valid JSON object in the raw text
        json_blocks = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw_text)
        for block in json_blocks:
            try:
                parsed = json.loads(block)
                if "action" in parsed:
                    if not parsed.get("thought"):
                        thought_match = re.search(r'<thought>(.*?)</thought>', raw_text, re.DOTALL)
                        if thought_match:
                            parsed["thought"] = thought_match.group(1).strip()
                    return parsed
            except json.JSONDecodeError:
                continue

        # Strategy 5: Regex extraction from completely garbled response
        thought = ""
        action = "observe"
        payload = {}

        # Extract thought from XML tags or "thought": "..." pattern
        thought_match = re.search(r'<thought>(.*?)</thought>', raw_text, re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()
        else:
            thought_match = re.search(r'"thought"\s*:\s*"(.*?)(?:"|$)', raw_text, re.DOTALL)
            if thought_match:
                thought = thought_match.group(1).strip()

        # Extract action
        action_match = re.search(r'"action"\s*:\s*"(\w+)"', raw_text)
        if action_match:
            action = action_match.group(1)

        # Extract payload based on action type — covers ALL 26 actions
        if action in ("post_message", "answer_user"):
            msg_match = re.search(r'"message"\s*:\s*"(.*?)(?:"\s*[,}]|$)', raw_text, re.DOTALL)
            if msg_match:
                payload = {"message": msg_match.group(1).strip()}

        elif action in ("inject_css", "inject_js"):
            code_key = "css" if action == "inject_css" else "code"
            code_match = re.search(rf'"{code_key}"\s*:\s*"(.*?)(?:"\s*[,}}]|$)', raw_text, re.DOTALL)
            if code_match:
                payload = {code_key: code_match.group(1).strip()}

        elif action in ("click", "type", "hover", "inspect_element", "capture_element"):
            sel_match = re.search(r'"selector"\s*:\s*"(.*?)(?:"|$)', raw_text)
            if sel_match:
                payload = {"selector": sel_match.group(1).strip()}
            if action == "type":
                text_match = re.search(r'"text"\s*:\s*"(.*?)(?:"|$)', raw_text)
                if text_match:
                    payload["text"] = text_match.group(1).strip()

        elif action in ("search_dom", "search_console", "search_network",
                        "search_playbook", "search_fixes", "search_conversations"):
            q_match = re.search(r'"query"\s*:\s*"(.*?)(?:"|$)', raw_text)
            if q_match:
                payload = {"query": q_match.group(1).strip()}

        elif action == "scroll":
            x_match = re.search(r'"x"\s*:\s*(-?\d+)', raw_text)
            y_match = re.search(r'"y"\s*:\s*(-?\d+)', raw_text)
            payload = {"x": int(x_match.group(1)) if x_match else 0,
                       "y": int(y_match.group(1)) if y_match else 0}

        elif action == "navigate":
            url_match = re.search(r'"url"\s*:\s*"(.*?)(?:"|$)', raw_text)
            if url_match:
                payload = {"url": url_match.group(1).strip()}

        elif action == "click_at_position":
            x_match = re.search(r'"x"\s*:\s*(-?\d+)', raw_text)
            y_match = re.search(r'"y"\s*:\s*(-?\d+)', raw_text)
            payload = {"x": int(x_match.group(1)) if x_match else 0,
                       "y": int(y_match.group(1)) if y_match else 0}

        elif action == "run_test":
            code_match = re.search(r'"code"\s*:\s*"(.*?)(?:"\s*[,}]|$)', raw_text, re.DOTALL)
            if code_match:
                payload = {"code": code_match.group(1).strip()}

        elif action == "read_network_body":
            fn_match = re.search(r'"filename"\s*:\s*"(.*?)(?:"|$)', raw_text)
            if fn_match:
                payload = {"filename": fn_match.group(1).strip()}

        elif action == "get_conversation_detail":
            fn_match = re.search(r'"filename"\s*:\s*"(.*?)(?:"|$)', raw_text)
            if fn_match:
                payload = {"filename": fn_match.group(1).strip()}

        elif action == "log_fix":
            entry_match = re.search(r'"entry"\s*:\s*"(.*?)(?:"\s*[,}]|$)', raw_text, re.DOTALL)
            if entry_match:
                payload = {"entry": entry_match.group(1).strip()}

        # observe, clear_site_data, diagnose — no payload needed

        if not thought:
            thought = f"[JSON_PARSE_ERROR] Raw response: {raw_text}"

        return {
            "thought": thought,
            "action": action,
            "payload": payload,
        }

    async def get_action(
        self,
        system_prompt: str,
        messages: list[dict],
        screenshot_base64: str | None = None,
    ) -> tuple[dict, str]:
        """Send context to AI model and get an action response.

        Retries up to MAX_RETRIES times with incremental backoff (3s, 6s, 9s, ...)
        on server errors (5xx), rate limits (429), and timeouts. Free-tier friendly.
        """
        api_messages = self._build_api_messages(system_prompt, messages, screenshot_base64)

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                    temperature=0.2,
                    max_tokens=4096,
                )
                raw_text = response.choices[0].message.content
                if not raw_text:
                    return {"thought": "[Empty response from model]", "action": "observe", "payload": {}}, ""
                raw_text = raw_text.strip()
                action_data = self._parse_json_response(raw_text)
                return action_data, raw_text

            except Exception as e:
                last_error = e

                if self._is_retryable(e) and attempt < MAX_RETRIES:
                    delay = BASE_DELAY * attempt  # 3, 6, 9, 12, 15, 18, 21, 24, 27
                    await self._countdown(delay, attempt, str(e))
                    continue
                else:
                    raise last_error
