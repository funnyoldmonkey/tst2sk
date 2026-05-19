"""Fix summarizer — single focused AI call for KB entry curation.

Called exactly ONCE per session when the user types "summarize".
Distills raw fix attempts into a clean KB entry with structured format:
  Store: ...
  Issue: ...
  Blockers: ...
  Code Fix: ...

Uses the same model + API keys as the main agent.
Falls back to deterministic entry builder if the AI call fails.
"""
import json
import asyncio
from openai import AsyncOpenAI
from rich.console import Console

from config import AppConfig

_console = Console()

SUMMARIZE_TIMEOUT = 120  # seconds — heavy thinking models need room


class SummarizerClient:
    """Lightweight AI caller for fix entry summarization."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.model = config.model

        # Mirror the same key list from the main client
        self._api_keys = config.api_keys if config.api_keys else [config.api_key]
        self._current_key_index = 0

        self.client = self._build_client(self._api_keys[self._current_key_index])

    def _build_client(self, api_key: str) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            base_url=self.config.base_url,
            default_headers=self.config.extra_headers if self.config.extra_headers else None,
        )

    def _rotate_key(self):
        """Rotate to the next API key on failure."""
        if len(self._api_keys) <= 1:
            return
        self._current_key_index = (self._current_key_index + 1) % len(self._api_keys)
        self.client = self._build_client(self._api_keys[self._current_key_index])

    async def build_fix_entry(
        self, url: str, query: str, fixes: list[dict], scenario: str, resolved: bool
    ) -> str | None:
        """Summarize raw fix attempts into a clean KB entry.

        Returns the formatted entry string, or None on failure (caller uses fallback).
        """
        _console.print(f"  [bold magenta]\U0001f9e0 Summarizing fixes...[/bold magenta]")

        system = """You are a knowledge base curator for a web debugging tool. Produce a CLEAN fix summary from raw session data.

Rules:
1. Store: the domain name
2. Issue: clear 1-2 sentence description of the symptom (not the user's raw input)
3. Blockers: what was preventing the fix or what caused the issue (CSS specificity, JS re-applying styles, missing elements, etc.)
4. Code Fix: ONLY the FINAL WORKING code — no console.log() debug calls, no diagnostic test queries, no intermediate attempts
5. If multiple DISTINCT fixes were applied (e.g., one CSS and one JS for different issues), list each separately
6. Identify actual fix injections vs diagnostic ones — keep ONLY working fixes

Output EXACTLY this format (one block per distinct fix):
---
Store: <domain>
Issue: <clean symptom description>
Blockers: <what caused/blocked it>
Code Fix:
  [inject_js] <final working JS code>
  [inject_css] <final working CSS code>
Verified: Yes/No
---

If there were multiple distinct fixes, output multiple blocks."""

        from datetime import datetime
        from urllib.parse import urlparse
        date_str = datetime.now().strftime("%Y-%m-%d")
        domain = "unknown"
        try:
            domain = urlparse(url).netloc if url else "unknown"
        except Exception:
            pass

        user_content = f"""Date: {date_str} | Domain: {domain} | Scenario: {scenario} | Resolved: {resolved}
Query: {query}

RAW INJECTIONS (includes debug attempts — filter to only working fixes):
{json.dumps(fixes, indent=2)[:5000]}"""

        try:
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.1,
                    max_tokens=1024,
                ),
                timeout=SUMMARIZE_TIMEOUT,
            )
            text = (response.choices[0].message.content or "").strip()
            if text and len(text) > 20:
                return text
            return None

        except asyncio.TimeoutError:
            _console.print(f"  [dim red]⚠️ Summarize timed out after {SUMMARIZE_TIMEOUT}s — using fallback[/dim red]")
            self._rotate_key()
            return None
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)[:150] if str(e) else "no details"
            _console.print(f"  [dim red]⚠️ Summarize failed ({error_type}): {error_msg} — using fallback[/dim red]")
            if "429" in str(e) or "rate" in str(e).lower():
                self._rotate_key()
            return None
