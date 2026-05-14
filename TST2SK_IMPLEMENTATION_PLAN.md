# TST2SK — Troubleshooting Tier 2 Sidekick
# Complete Implementation Plan

## What Is TST2SK?

TST2SK is a standalone Python application that autonomously troubleshoots web pages. It opens a real browser, captures the full page state (DOM, console logs, network requests, screenshot), sends everything to an AI model via OpenAI-compatible API, receives an action back, executes it in the browser, and repeats. No browser extension, no IDE, no manual setup — just Python, an API key, and a URL.

The user runs the app, picks their AI provider (OpenRouter, Google AI Studio, OpenAI, any OpenAI-compatible endpoint), enters a URL and their concern, and the system autonomously investigates and fixes the page while the user watches a live terminal UI showing what the AI is doing.

## Tech Stack

- **Python 3.11+** — the entire app
- **Playwright** (via `playwright` Python package) — browser automation with full Chrome DevTools Protocol (CDP) access. This gives us: screenshots, DOM extraction, console log capture, network monitoring, JavaScript execution (bypasses CSP via CDP), CSS injection, click/type/scroll, and element inspection. No extension needed.
- **OpenAI Python SDK** (`openai` package) — OpenAI-compatible client that works with ANY provider (OpenRouter, Google, Anthropic, local models) by changing `base_url` and `api_key`
- **Rich** (`rich` package) — terminal UI for live status, colored output, progress indicators
- **Pillow** (`Pillow` package) — image cropping for capture_element
- **Python standard library** — `json`, `re`, `os`, `asyncio`, `base64`, `hashlib`, `time`, `pathlib`

**Why Playwright over browser-use:** browser-use adds its own AI agent layer and abstracts away CDP access. We need raw CDP for: console log capture, network request interception, CSP-bypassing JS injection, and response body retrieval. Playwright gives us all of this directly. browser-use would be an unnecessary middleman that removes control we need.

## Directory Structure

```
tst2sk/
├── main.py                      # Entry point — CLI setup, config wizard, starts the loop
├── config.py                    # Configuration dataclass + .env loading + provider presets
├── browser/
│   ├── __init__.py              # Empty
│   ├── controller.py            # Browser lifecycle (launch, connect CDP, close)
│   ├── observer.py              # Captures full page state (DOM, console, network, screenshot)
│   └── actions.py               # Executes AI actions in the browser (click, inject_js, etc.)
├── ai/
│   ├── __init__.py              # Empty
│   ├── client.py                # OpenAI-compatible API wrapper with vision support
│   └── prompts.py               # System prompt (the brain protocol adapted for TST2SK)
├── engine/
│   ├── __init__.py              # Empty
│   ├── brain.py                 # The agent loop: observe → think → act → repeat
│   ├── diagnostics.py           # Cross-reference diagnostic engine (diagnose action)
│   ├── search.py                # Local search: search_dom, search_console, search_network
│   └── kb.py                    # Knowledge base: log_fix, search_fixes, find_relevant
├── playbooks/
│   └── PLAYBOOKS.md             # Fix recipes (copy from existing project)
├── kb/
│   └── fixes.log                # Knowledge base file (created on first log_fix)
├── scratch/                     # Runtime observation data (created automatically)
│   ├── obs_dom.txt
│   ├── obs_console.log
│   ├── obs_network.log
│   ├── obs_net_bodies/
│   ├── current_view.png
│   └── element_capture.png
├── requirements.txt
└── .env.example                 # Example config file
```

---

## PHASE 1: Project Scaffolding

### Step 1.1: Create directory structure

Create every directory and empty `__init__.py` file listed above. The `kb/`, `scratch/`, and `scratch/obs_net_bodies/` directories should be created at runtime by the code, not pre-created.

### Step 1.2: Create `requirements.txt`

```
playwright>=1.40.0
openai>=1.12.0
rich>=13.7.0
Pillow>=10.0.0
python-dotenv>=1.0.0
```

### Step 1.3: Create `.env.example`

```
# AI Provider Configuration
# Works with any OpenAI-compatible API: OpenRouter, Google AI Studio, OpenAI, local models
AI_API_KEY=your-api-key-here
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-2.0-flash-001

# Optional: custom headers for specific providers
# AI_EXTRA_HEADERS={"HTTP-Referer": "https://tst2sk.app"}
```

### Step 1.4: Create `config.py`

```python
"""Configuration for TST2SK."""
import os
import json
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

PROVIDER_PRESETS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "google/gemini-2.0-flash-001",
            "google/gemma-3-27b-it",
            "anthropic/claude-sonnet-4",
            "meta-llama/llama-4-maverick",
        ],
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            "gemini-2.0-flash",
            "gemini-2.5-flash-preview-05-20",
            "gemma-3-27b-it",
        ],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "custom": {
        "base_url": "",
        "models": [],
    },
}

@dataclass
class AppConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    extra_headers: dict = field(default_factory=dict)
    headless: bool = False  # Show browser window by default
    max_turns: int = 30  # Safety limit on agent loop iterations
    max_history: int = 20  # Max conversation turns to keep in context

    @classmethod
    def from_env(cls) -> "AppConfig":
        headers = {}
        raw_headers = os.getenv("AI_EXTRA_HEADERS", "")
        if raw_headers:
            try:
                headers = json.loads(raw_headers)
            except json.JSONDecodeError:
                pass
        return cls(
            api_key=os.getenv("AI_API_KEY", ""),
            base_url=os.getenv("AI_BASE_URL", ""),
            model=os.getenv("AI_MODEL", ""),
            extra_headers=headers,
        )
```

---

## PHASE 2: Browser Controller

This is the core. Playwright launches a Chromium browser with CDP access. We attach event listeners for console logs and network requests. We capture DOM, screenshots, and can execute JavaScript via CDP (bypassing Content Security Policy).

### Step 2.1: Create `browser/controller.py`

This file manages the browser lifecycle.

```python
"""Browser lifecycle management using Playwright + CDP."""
import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, CDPSession

class BrowserController:
    """Manages a Chromium browser instance with CDP access."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp: CDPSession | None = None

        # Diagnostic storage — populated by event listeners
        self.console_logs: list[str] = []
        self.network_log: list[str] = []
        self.network_bodies: dict[str, str] = {}  # url -> response body (for interesting URLs)

    async def launch(self):
        """Launch browser, create page, attach CDP, start listeners."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
        self.page = await self._context.new_page()

        # Attach CDP session for low-level access
        self.cdp = await self.page.context.new_cdp_session(self.page)
        await self.cdp.send("Network.enable")
        await self.cdp.send("Runtime.enable")

        # --- Console log listener ---
        self.page.on("console", self._on_console)

        # --- Network listeners ---
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

    def _on_console(self, msg):
        """Capture console messages."""
        level = msg.type  # 'log', 'error', 'warning', 'info', etc.
        text = msg.text
        self.console_logs.append(f"[{level}] {text}")

    async def _on_response(self, response):
        """Capture network responses."""
        url = response.url
        status = response.status
        if status >= 400:
            self.network_log.append(f"🚨 FAILED: {url} ({status})")
        else:
            is_interesting = (
                "/api/" in url or ".json" in url or "cart" in url
            )
            if is_interesting:
                self.network_log.append(f"📦 DATA [{url.split('?')[0].split('/')[-1]}]: (body available)")
                try:
                    body = await response.text()
                    self.network_bodies[url] = body
                except Exception:
                    pass
            else:
                self.network_log.append(f"✅ SUCCESS: {url.split('?')[0]} ({status})")

    def _on_request_failed(self, request):
        """Capture failed network requests."""
        url = request.url
        failure = request.failure
        self.network_log.append(f"🚨 FAILED: {url} (network error: {failure})")

    async def navigate(self, url: str):
        """Navigate to a URL and wait for load."""
        self.console_logs.clear()
        self.network_log.clear()
        self.network_bodies.clear()
        await self.page.goto(url, wait_until="networkidle", timeout=30000)

    def clear_diagnostics(self):
        """Snapshot and clear diagnostics for the current turn."""
        logs = list(self.console_logs)
        network = list(self.network_log)
        bodies = dict(self.network_bodies)
        self.console_logs.clear()
        self.network_log.clear()
        # Don't clear network_bodies — they accumulate for get_network_body lookups
        return logs, network, bodies

    async def close(self):
        """Clean shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
```

### Step 2.2: Create `browser/observer.py`

This captures the full page state: DOM tree, screenshot, console logs, network. It mirrors what the Chrome extension's `captureObservation()` does but using Playwright.

```python
"""Page observation — captures DOM, screenshot, console, network."""
import base64
from browser.controller import BrowserController

async def capture_observation(browser: BrowserController) -> dict:
    """Capture the full page state. Returns a dict with dom, console, network, screenshot_base64, url."""
    page = browser.page

    # 1. Capture DOM — same logic as the extension's describe() function
    dom_text = await page.evaluate("""() => {
        const describe = (el) => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'script') {
                const src = el.src || el.getAttribute('src');
                if (src) return '📜 [script] src="' + src + '"';
                const inline = (el.textContent || '').trim().substring(0, 200);
                if (inline) return '📜 [script:inline] "' + inline + '..."';
                return null;
            }
            if (tag === 'link') {
                const href = el.href || el.getAttribute('href');
                const rel = el.rel || '';
                if (href) return '🎨 [link rel="' + rel + '"] href="' + href + '"';
                return null;
            }
            if (tag === 'style') {
                const len = (el.textContent || '').length;
                const id = el.id ? '#' + el.id : '';
                return '🎨 [style' + id + '] (' + len + ' chars)';
            }
            if (['meta', 'noscript', 'br', 'hr'].includes(tag)) return null;

            const id = el.id ? '#' + el.id : '';
            const cls = el.className && typeof el.className === 'string'
                ? '.' + el.className.split(' ').join('.') : '';
            const type = el.type ? '[type="' + el.type + '"]' : '';

            const interactive = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'];
            const hasClick = el.onclick || el.getAttribute('role') === 'button';
            let isInteractive = interactive.includes(el.tagName) || hasClick;

            const text = (el.innerText || el.value || el.title || '').trim();
            const directText = isInteractive ? text : Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .filter(t => t.length > 0)
                .join(' ');

            let visFlag = '';
            if (isInteractive || id || directText) {
                try {
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none') visFlag = ' [HIDDEN:display]';
                    else if (cs.visibility === 'hidden') visFlag = ' [HIDDEN:visibility]';
                    else if (cs.opacity === '0') visFlag = ' [HIDDEN:opacity]';
                    else if (el.offsetWidth === 0 && el.offsetHeight === 0 && !isInteractive)
                        visFlag = ' [HIDDEN:zero-size]';
                    if (!isInteractive && cs.cursor === 'pointer') isInteractive = true;
                } catch(e) {}
            }

            if (!directText && !isInteractive && !id && !visFlag) return null;

            const marker = isInteractive ? '★' : '·';
            return marker + ' [' + tag + id + cls + type + ']' + visFlag + ' "' + directText + '"';
        };
        return Array.from(document.querySelectorAll('*'))
            .map(describe)
            .filter(x => x !== null)
            .join('\\n');
    }""")

    # 2. Screenshot as base64
    screenshot_bytes = await page.screenshot(full_page=False, type="png")
    screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    # 3. Snapshot diagnostics (console + network) and clear for next turn
    console_logs, network_log, _ = browser.clear_diagnostics()

    # 4. Current URL
    url = page.url

    return {
        "dom": dom_text,
        "console": "\n".join(console_logs) if console_logs else "No console logs.",
        "network": "\n".join(network_log) if network_log else "No network activity.",
        "screenshot_base64": screenshot_base64,
        "url": url,
    }
```

### Step 2.3: Create `browser/actions.py`

This executes AI-decided actions in the browser. Each action returns a status string that gets logged.

```python
"""Execute AI actions in the browser."""
import base64
import io
from PIL import Image
from browser.controller import BrowserController

async def execute_action(browser: BrowserController, action: str, payload: dict) -> str:
    """Execute a single action. Returns a status message string."""
    page = browser.page
    cdp = browser.cdp

    try:
        if action == "click":
            selector = payload["selector"]
            el = await page.query_selector(selector)
            if not el:
                return f"[error] click: selector not found — \"{selector}\""
            await el.scroll_into_view_if_needed()
            await el.click()
            return f"Clicked: {selector}"

        elif action == "type":
            selector = payload["selector"]
            text = payload["text"]
            el = await page.query_selector(selector)
            if not el:
                return f"[error] type: selector not found — \"{selector}\""
            await el.focus()
            await el.fill(text)
            return f"Typed into: {selector}"

        elif action == "scroll":
            x = payload.get("x", 0)
            y = payload.get("y", 0)
            await page.evaluate(f"window.scrollBy({x}, {y})")
            return f"Scrolled by ({x}, {y})"

        elif action == "hover":
            selector = payload["selector"]
            el = await page.query_selector(selector)
            if not el:
                return f"[error] hover: selector not found — \"{selector}\""
            await el.hover()
            return f"Hovered: {selector}"

        elif action == "navigate":
            url = payload["url"]
            browser.console_logs.clear()
            browser.network_log.clear()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            return f"Navigated to: {url}"

        elif action == "inject_js":
            code = payload.get("code", "")
            # Use CDP Runtime.evaluate to bypass CSP
            result = await cdp.send("Runtime.evaluate", {
                "expression": code,
                "userGesture": True,
                "awaitPromise": True,
            })
            if "exceptionDetails" in result:
                err = result["exceptionDetails"].get("exception", {}).get("description", "Unknown error")
                browser.console_logs.append(f"[error] Script exception: {err}")
                return f"[error] inject_js exception: {err}"
            return "inject_js executed successfully"

        elif action == "inject_css":
            css = payload.get("css", "")
            await page.add_style_tag(content=css)
            return "inject_css applied"

        elif action == "inspect_element":
            selector = payload["selector"]
            result = await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (!el) return "NOT_FOUND";
                const r = el.getBoundingClientRect();
                const cs = window.getComputedStyle(el);
                return {
                    tag: el.tagName, id: el.id, classes: el.className,
                    value: el.value || "",
                    rect: { x: r.left, y: r.top, w: r.width, h: r.height },
                    styles: {
                        display: cs.display, visibility: cs.visibility,
                        opacity: cs.opacity, color: cs.color,
                        fontSize: cs.fontSize, zIndex: cs.zIndex,
                        filter: cs.filter, pointerEvents: cs.pointerEvents,
                        clipPath: cs.clipPath, transform: cs.transform,
                        maxHeight: cs.maxHeight, cursor: cs.cursor,
                    },
                    attributes: Array.from(el.attributes).reduce((acc, attr) => {
                        acc[attr.name] = attr.value; return acc;
                    }, {})
                };
            }""", selector)
            browser.console_logs.append(f">>> INSPECT [{selector}]: {result}")
            return f"inspect_element result logged to console"

        elif action == "run_test":
            code = payload.get("code", "")
            wrapped = f"(function(){{ try {{ {code}\n return {{ success: true, message: 'Test Passed' }}; }} catch(e) {{ return {{ success: false, message: e.message }}; }} }})()"
            result = await page.evaluate(wrapped)
            browser.console_logs.append(f">>> TEST_RESULT: {result}")
            return f"run_test: {result}"

        elif action == "observe":
            # No action — just triggers a new observation on the next loop iteration
            return "observe requested — fresh observation coming"

        elif action == "get_network_body":
            url_query = payload.get("url", "")
            # Search accumulated bodies
            for stored_url, body in browser.network_bodies.items():
                if url_query in stored_url:
                    browser.console_logs.append(f">>> NETWORK_BODY [{url_query}]: {body[:5000]}")
                    return f"Network body retrieved for {url_query}"
            browser.console_logs.append(f">>> NETWORK_BODY [{url_query}]: NOT_FOUND")
            return f"Network body not found for {url_query}"

        elif action == "clear_site_data":
            await browser.page.context.clear_cookies()
            await page.evaluate("localStorage.clear(); sessionStorage.clear();")
            browser.console_logs.append("🧹 SITE DATA CLEARED")
            return "Site data cleared — navigate to reload"

        elif action == "capture_element":
            selector = payload["selector"]
            result = await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { x: r.left, y: r.top, w: r.width, h: r.height, dpr: window.devicePixelRatio };
            }""", selector)
            if not result:
                browser.console_logs.append(f"[error] capture_element: selector not found — \"{selector}\"")
                return f"[error] capture_element: selector not found"

            # Take full page screenshot and crop
            screenshot_bytes = await page.screenshot(full_page=False, type="png")
            img = Image.open(io.BytesIO(screenshot_bytes))
            dpr = result.get("dpr", 1) or 1
            x = int(result["x"] * dpr)
            y = int(result["y"] * dpr)
            x2 = min(img.width, x + int(result["w"] * dpr))
            y2 = min(img.height, y + int(result["h"] * dpr))
            if x2 > x and y2 > y:
                cropped = img.crop((x, max(0, y), x2, y2))
                crop_path = "scratch/element_capture.png"
                cropped.save(crop_path, "PNG")
                browser.console_logs.append(
                    f"🔍 ELEMENT CAPTURED: {selector} at {result['x']},{result['y']} "
                    f"({result['w']}×{result['h']}) — cropped to scratch/element_capture.png"
                )
            return f"capture_element done: {selector}"

        elif action == "click_at_position":
            x = payload["x"]
            y = payload["y"]
            await page.mouse.click(x, y)
            return f"Clicked at position ({x}, {y})"

        elif action == "post_message" or action == "answer_user":
            # The AI is speaking to the user — we just display it
            message = payload.get("message", payload.get("text", ""))
            return f"MESSAGE_TO_USER: {message}"

        elif action == "log_fix":
            # Handled by the engine, not the browser
            return "log_fix handled by engine"

        else:
            return f"[error] Unknown action: {action}"

    except Exception as e:
        return f"[error] Action {action} failed: {str(e)}"
```

---

## PHASE 3: AI Client

### Step 3.1: Create `ai/client.py`

Wraps the OpenAI SDK for vision-capable chat completions. Works with any OpenAI-compatible API.

```python
"""OpenAI-compatible AI client with vision support."""
import json
from openai import AsyncOpenAI
from config import AppConfig

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

    async def get_action(
        self,
        system_prompt: str,
        messages: list[dict],
        screenshot_base64: str | None = None,
    ) -> dict:
        """Send context to AI model and get an action response.

        Args:
            system_prompt: The brain protocol
            messages: Conversation history (list of {"role": ..., "content": ...})
            screenshot_base64: Current screenshot as base64 PNG string

        Returns:
            Parsed action dict: {"thought": ..., "action": ..., "payload": ...}
        """
        # Build the messages array for the API call
        api_messages = [{"role": "system", "content": system_prompt}]

        # Add history messages
        for msg in messages:
            api_messages.append(msg)

        # The last user message should include the screenshot as a vision image
        # We modify the last user message to include the image
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

        # Call the API
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            temperature=0.2,
            max_tokens=4096,
        )

        raw_text = response.choices[0].message.content.strip()

        # Parse JSON from the response — handle markdown code blocks
        json_text = raw_text
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0].strip()

        # Try to find JSON object in the text
        start = json_text.find("{")
        end = json_text.rfind("}") + 1
        if start != -1 and end > start:
            json_text = json_text[start:end]

        try:
            action_data = json.loads(json_text)
        except json.JSONDecodeError:
            # Return the raw text as a thought with observe action so the loop continues
            action_data = {
                "thought": f"[JSON_PARSE_ERROR] Raw response: {raw_text[:500]}",
                "action": "observe",
                "payload": {},
            }

        return action_data, raw_text
```

### Step 3.2: Create `ai/prompts.py`

The system prompt — adapted from TS_SIDEKICK_BRAIN.md for the standalone architecture. Remove all references to brain_input.json, brain_output.json, IDE, extension, file-based workflow. Replace with direct function-call style.

```python
"""System prompt for TST2SK AI brain."""

SYSTEM_PROMPT = """# TST2SK: Elite Autonomous Tier 2 Troubleshooting Agent

You are TST2SK, an autonomous Tier 2 support agent. You operate in a continuous loop. The system captures the page state (DOM, console, network, screenshot) and sends it to you. You analyze everything and respond with ONE action to execute. After your action runs, you get a fresh observation. Repeat until fixed.

## GOLDEN RULE: SILENT UNTIL SOLVED
Do NOT use post_message until you have either:
- A verified working fix (confirmed via screenshot + DOM check), OR
- Exhausted 3+ fix attempts and need user input.
Work silently. The user sees your thoughts but doesn't need play-by-play.

## SCREENSHOT — Your Most Important Tool
Every observation includes the page screenshot. **YOU MUST actually look at the screenshot every turn.** Describe what you see in your thought. Use visual evidence to drive your investigation:
1. **Identify** what's visually wrong — missing content, broken layout, invisible elements, error banners.
2. **Cross-reference** what you see against DOM/console data. If DOM says elements exist but screenshot shows blank, elements are hidden.
3. **Let the screenshot drive your next action.** If you see prices missing, search for price elements. If you see the page is blank, check positioning.
**MANDATORY: Your thought MUST start with "Screenshot shows: [what you see]" every turn.** If you skip this, you are working blind.

## CONSOLE ERRORS — Verify, Don't Trust Blindly
Console errors can be FAKE or MISLEADING. Apps and scripts can print anything to console. Before accepting a console error as the root cause:
- **Cross-reference with DOM evidence** — does the DOM confirm what the error claims?
- **Cross-reference with the screenshot** — does the page look like what the error describes?
- **Check if the error source exists** — if an error references "StockSync" but no StockSync script is in the DOM, the error is planted.
- **Never diagnose based on console errors alone.** Always verify with visual + DOM evidence.

## Universal Diagnostic Framework

### Step 1: LOOK at the screenshot + run diagnose
Before anything else, describe what the screenshot shows. Then run `diagnose` to get the full diagnosis packet.

### Step 2: Search playbooks AND knowledge base
Always search both sources before attempting any fix — they are references, not orders. If your investigation tells you the root cause is different from what the playbook or KB entry describes, trust your investigation and pivot.

**a) Playbook recipes:** Use `search_playbook(query)` with symptom keywords.
**b) Past verified fixes:** Check `relevant_fixes` in the observation. Also use `search_fixes(query)` with symptom keywords.

**How to USE what you find:**
- Compare the root cause — does the KB entry describe the SAME root cause you're seeing? If not, skip it.
- If nothing matches, investigate from scratch. Not every ticket has a precedent.

### Step 3: Deep investigation with search tools
Use `search_dom`, `search_console`, `search_network`, `read_network_body` for targeted lookups.

### Step 4: Fix → LOOK at the screenshot → Verify → Iterate
After every fix, LOOK at the screenshot and describe what changed. Run `run_test` with visual assertions. Both must pass.

## Fix-and-Verify Loop Protocol
1. Check `relevant_fixes` — if root cause matches, try that approach. If different, skip.
2. Thought MUST include: what you're fixing, why, how you'll verify.
3. `inject_css` or `inject_js` with the fix — **CSS first** (see escalation order).
4. After EVERY fix:
   a. Look at the screenshot — describe what changed.
   b. Run `run_test` — check VISUAL computed styles (opacity, fontSize, clipPath, transform, zIndex, pointerEvents, getBoundingClientRect).
   c. Both screenshot AND test must pass.
5. If NOT fixed: try a DIFFERENT approach. Re-search playbook and KB with updated keywords.
6. **Escalation order — MUST follow:**
   - **Level 1: inject_css** — CSS-only fix first. Most visibility/layout issues solve here.
   - **Level 2: Targeted inject_js** — If CSS failed. Fix ONE thing per injection.
     - Level 2 as first attempt OK ONLY for: disabled buttons, event handlers, form logic, variant IDs, fetch/API, script re-init.
   - **Level 3: DOM reconstruction** — Only if targeted fixes keep failing.
   - **Level 4: User notification** — After 3 failed attempts.
   - **⚠️ One fix per injection.** Never combine unrelated fixes. Verify each separately.
   - **⚠️ Gate check before ANY inject_js:** "Is this a visibility/layout issue? Did I try inject_css first?"
7. MutationObserver rules:
   - Default: don't use one. Most fixes are one-shot.
   - If needed, scope narrowly: watch ONE element, not document.body.
   - **NEVER:** `observer.observe(document.body, { subtree: true, childList: true })`
8. After 3 failed attempts, use post_message with root cause + what was tried + what user needs to do.

## Delivering the Fix
Once verified:
1. Root cause — one sentence.
2. The fix code — clean, copyable.
3. Where to implement permanently.
4. Screenshot confirmation.
5. **MANDATORY: Ask "Is everything looking good? Can I close out this session?"**

## Actions — Complete Reference

### Browser Actions (each triggers a fresh observation after execution)
- `click` — payload: { "selector": "..." }
- `type` — payload: { "selector": "...", "text": "..." }
- `scroll` — payload: { "x": 0, "y": 500 }
- `hover` — payload: { "selector": "..." }
- `navigate` — payload: { "url": "..." }
- `inject_js` — payload: { "code": "..." } — Executes via CDP, bypasses CSP.
- `inject_css` — payload: { "css": "..." }
- `inspect_element` — payload: { "selector": "..." } — Returns styles, rect, attributes in console.
- `run_test` — payload: { "code": "..." } — Executes in try/catch. Throw to fail. Check getComputedStyle for VISUAL properties.
- `observe` — payload: {} — Get fresh screenshot without doing anything.
- `get_network_body` — payload: { "url": "..." } — Read response body of a captured request.
- `clear_site_data` — payload: { "url": "..." } — Clears cookies/localStorage. Follow with navigate.
- `capture_element` — payload: { "selector": "..." } — Crops screenshot to element.
- `click_at_position` — payload: { "x": 0, "y": 0 }
- `post_message` — payload: { "message": "..." } — Speak to the user. Only after verified fix or 3 failures.

### Search Actions (instant, no browser roundtrip)
- `diagnose` — payload: {} — Full cross-reference + scenario detection. START HERE.
- `search_dom` — payload: { "query": "price|[HIDDEN" } — Regex/pipe-separated search of DOM.
- `search_console` — payload: { "query": "error|failed" }
- `search_network` — payload: { "query": "/cart/add|FAILED" }
- `read_network_body` — payload: { "filename": "cart_add" } or {} to list files.
- `search_playbook` — payload: { "query": "add to cart|button" }
- `search_fixes` — payload: { "query": "opacity|disabled" }
- `log_fix` — payload: { "entry": "---\\n[date] store: ...\\n..." } — Only after user confirms.

## Output Format — STRICT
You MUST respond with ONLY a JSON object. No markdown, no explanation outside the JSON.
```
{
  "thought": "Screenshot shows: [describe what you see]. [Your reasoning, evidence, plan]",
  "action": "action_name",
  "payload": { ... }
}
```
"""
```

---

## PHASE 4: Diagnostic Engine

### Step 4.1: Create `engine/search.py`

Local search functions — identical logic to the existing `_grep_file` in main.py.

```python
"""Local search functions for DOM, console, network files."""
import os
import re

SCRATCH_DIR = "scratch"

def search_file(filepath: str, query: str) -> dict:
    """Search a file for lines matching a query. Supports regex and pipe-separated OR."""
    if not os.path.exists(filepath):
        return {"error": f"File not found: {filepath}", "matches": []}

    matches = []
    try:
        try:
            pattern = re.compile(query, re.IGNORECASE)
            use_regex = True
        except re.error:
            use_regex = False

        with open(filepath, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                hit = pattern.search(line) if use_regex else (query.lower() in line.lower())
                if hit:
                    matches.append({"line": i, "content": line.rstrip()})
    except Exception as e:
        return {"error": str(e), "matches": []}

    return {
        "query": query,
        "file": os.path.basename(filepath),
        "total_matches": len(matches),
        "matches": matches[:100],
        "truncated": len(matches) > 100,
    }

def search_dom(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_dom.txt"), query)

def search_console(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_console.log"), query)

def search_network(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_network.log"), query)

def read_network_body(filename: str = "") -> dict:
    bodies_dir = os.path.join(SCRATCH_DIR, "obs_net_bodies")
    if not os.path.exists(bodies_dir):
        return {"available_files": [], "hint": "No network bodies captured yet."}

    if filename:
        for f in os.listdir(bodies_dir):
            if filename.lower() in f.lower():
                with open(os.path.join(bodies_dir, f), "r", encoding="utf-8") as fh:
                    return {"file": f, "body": fh.read()[:10000]}

    return {
        "available_files": os.listdir(bodies_dir),
        "hint": "Provide a filename or partial match.",
    }
```

### Step 4.2: Create `engine/diagnostics.py`

Port the `cross_reference_diagnostics()` function from main.py. This is a direct copy of the logic — the function reads from scratch files and returns a structured diagnosis.

```python
"""Cross-reference diagnostic engine — auto-detects scenario type."""
import os
import re
from urllib.parse import urlparse

SCRATCH_DIR = "scratch"

def cross_reference_diagnostics() -> dict:
    """EXACT PORT of the cross_reference_diagnostics() function from main.py.
    Reads scratch/obs_dom.txt, scratch/obs_console.log, scratch/obs_network.log.
    Returns structured diagnosis with detected_scenario, platform, scripts,
    hidden_elements, console_errors, failed_requests, forms, auth_signals,
    third_party_embeds, potential_issues, shopify_context, scenario_ranking, summary."""

    # *** COPY THE ENTIRE cross_reference_diagnostics() FUNCTION FROM server/main.py ***
    # *** Lines 524-853 of server/main.py — copy it verbatim ***
    # *** Only change: replace os.path.join(SCRATCH_DIR, ...) paths to use local SCRATCH_DIR ***
    # *** The function reads from files, does not need any server or extension ***

    # [PASTE THE FULL FUNCTION BODY HERE FROM main.py lines 524-853]
    pass  # PLACEHOLDER — replace with full function
```

**IMPORTANT FOR THE BUILDER:** Copy the ENTIRE `cross_reference_diagnostics()` function body from `server/main.py` (lines 524-853). It reads from scratch files and returns a dict. Only change the `SCRATCH_DIR` reference to point to the local `scratch/` directory. Also copy the helper variables (`SCRATCH_NET_BODIES` path).

### Step 4.3: Create `engine/kb.py`

Knowledge base operations — port from main.py.

```python
"""Knowledge base — log fixes, search fixes, find relevant."""
import os
import re
import time
from pathlib import Path

KB_DIR = "kb"
KB_FIXES_LOG = os.path.join(KB_DIR, "fixes.log")
PLAYBOOKS_PATH = os.path.join("playbooks", "PLAYBOOKS.md")

def ensure_kb_dir():
    os.makedirs(KB_DIR, exist_ok=True)

def append_fix(entry_text: str) -> dict:
    """Append a verified fix entry to kb/fixes.log."""
    ensure_kb_dir()
    try:
        with open(KB_FIXES_LOG, "a", encoding="utf-8") as f:
            f.write(entry_text.strip() + "\n")
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

def search_fixes(query: str) -> dict:
    """Search kb/fixes.log for entries matching query. Supports regex and pipe-separated OR.
    COPY the _search_fixes() function from main.py lines 431-483."""
    # [PASTE FROM main.py]
    pass  # PLACEHOLDER

def search_playbook(query: str) -> dict:
    """Search PLAYBOOKS.md for fix recipes. Returns up to 3 matching sections.
    COPY the _search_playbook() function from main.py lines 350-428."""
    # [PASTE FROM main.py]
    pass  # PLACEHOLDER

def find_relevant_fixes(scenario: str, url: str, diagnosis_hints: list = None) -> list:
    """Auto-search KB for entries matching scenario/URL/hints.
    COPY the find_relevant_fixes() function from main.py lines 870-930."""
    # [PASTE FROM main.py]
    pass  # PLACEHOLDER
```

**IMPORTANT FOR THE BUILDER:** Copy the function bodies from main.py. They are pure Python with no server dependencies — they just read files and return dicts.

---

## PHASE 5: The Brain — Agent Loop

### Step 5.1: Create `engine/brain.py`

This is the main agent loop. It ties everything together.

```python
"""The agent brain — observe, think, act, repeat."""
import os
import re
import json
import time
import hashlib
import asyncio
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from config import AppConfig
from browser.controller import BrowserController
from browser.observer import capture_observation
from browser.actions import execute_action
from ai.client import AIClient
from ai.prompts import SYSTEM_PROMPT
from engine.diagnostics import cross_reference_diagnostics
from engine.search import search_dom, search_console, search_network, read_network_body
from engine.kb import append_fix, search_fixes, search_playbook, find_relevant_fixes

console = Console()
SCRATCH_DIR = "scratch"
SCRATCH_NET_BODIES = os.path.join(SCRATCH_DIR, "obs_net_bodies")

# Actions that are handled locally (no browser roundtrip)
LOCAL_ACTIONS = {
    "search_dom", "search_console", "search_network",
    "read_network_body", "diagnose", "log_fix",
    "search_playbook", "search_fixes",
}

class Brain:
    """The autonomous troubleshooting agent loop."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.browser = BrowserController(headless=config.headless)
        self.ai = AIClient(config)
        self.messages: list[dict] = []  # Conversation history for the AI
        self.fix_attempts: list[dict] = []
        self.turn_count = 0
        self.detected_scenario = ""
        self.diagnosis_hints: list[str] = []

    async def start(self, url: str, user_query: str):
        """Main entry point — navigate to URL and start the loop."""
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        os.makedirs(SCRATCH_NET_BODIES, exist_ok=True)

        console.print(Panel(f"🚀 Starting TST2SK session\n📍 URL: {url}\n❓ Query: {user_query}", style="green"))

        await self.browser.launch()
        console.print("[green]Browser launched[/green]")

        await self.browser.navigate(url)
        console.print(f"[green]Navigated to {url}[/green]")

        # Wait a moment for page to settle
        await asyncio.sleep(2)

        # Capture initial observation
        obs = await capture_observation(self.browser)
        self._write_scratch_files(obs)

        # Build initial context message for the AI
        slim_obs = self._build_slim_observation(obs)
        initial_context = self._build_context_message(slim_obs, user_query, obs["url"])

        self.messages.append({"role": "user", "content": initial_context})

        # Start the agent loop
        await self._loop(obs["screenshot_base64"])

    async def _loop(self, current_screenshot: str):
        """The main observe → think → act → repeat loop."""
        while self.turn_count < self.config.max_turns:
            self.turn_count += 1
            console.print(f"\n[bold cyan]═══ Turn {self.turn_count} ═══[/bold cyan]")

            # Trim history to prevent context overflow
            self._trim_history()

            # Get action from AI
            try:
                action_data, raw_response = await self.ai.get_action(
                    SYSTEM_PROMPT,
                    self.messages,
                    current_screenshot,
                )
            except Exception as e:
                console.print(f"[red]AI API error: {e}[/red]")
                await asyncio.sleep(2)
                continue

            thought = action_data.get("thought", "")
            action = action_data.get("action", "observe")
            payload = action_data.get("payload", {})

            # Display thought
            console.print(Panel(thought[:500], title="💭 Thought", style="yellow"))
            console.print(f"[bold]Action:[/bold] {action}")

            # Add AI response to history
            self.messages.append({"role": "assistant", "content": raw_response})

            # --- Handle post_message (AI speaking to user) ---
            if action in ("post_message", "answer_user"):
                message = payload.get("message", payload.get("text", ""))
                console.print(Panel(message, title="🤖 Agent Message", style="bold green"))

                # Ask user if they want to continue
                user_input = input("\n[You] > ").strip()
                if user_input.lower() in ("yes", "looks good", "all good", "done", "close"):
                    console.print("[green]Session complete![/green]")
                    break
                else:
                    # User wants more work — add their response and continue
                    self.messages.append({"role": "user", "content": user_input})
                    # Get fresh observation
                    obs = await capture_observation(self.browser)
                    self._write_scratch_files(obs)
                    slim_obs = self._build_slim_observation(obs)
                    context = self._build_context_message(slim_obs, user_input, obs["url"])
                    self.messages.append({"role": "user", "content": context})
                    current_screenshot = obs["screenshot_base64"]
                    continue

            # --- Handle local actions (no browser needed) ---
            if action in LOCAL_ACTIONS:
                result = self._handle_local_action(action, payload)
                console.print(f"[dim]Local result: {json.dumps(result, indent=2)[:300]}[/dim]")

                # Add result as a new user message (observation)
                result_msg = f"Action result for {action}:\n```json\n{json.dumps(result, indent=2)[:3000]}\n```"
                self.messages.append({"role": "user", "content": result_msg})
                # No new screenshot needed for local actions
                continue

            # --- Handle browser actions ---
            # Track fix attempts
            if action in ("inject_js", "inject_css"):
                self._record_fix_attempt(action_data)

            result = await execute_action(self.browser, action, payload)
            console.print(f"[dim]Result: {result}[/dim]")

            # Wait for page to settle after action
            if action in ("navigate", "click"):
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(1.5)

            # Capture fresh observation
            obs = await capture_observation(self.browser)
            self._write_scratch_files(obs)
            slim_obs = self._build_slim_observation(obs)

            # Build context message with observation
            context = self._build_observation_message(slim_obs, result, obs["url"])
            self.messages.append({"role": "user", "content": context})
            current_screenshot = obs["screenshot_base64"]

        if self.turn_count >= self.config.max_turns:
            console.print(f"[red]Max turns ({self.config.max_turns}) reached. Stopping.[/red]")

    def _handle_local_action(self, action: str, payload: dict) -> dict:
        """Handle actions that don't need the browser."""
        query = payload.get("query", "")

        if action == "diagnose":
            result = cross_reference_diagnostics()
            self.detected_scenario = result.get("detected_scenario", "")
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
        elif action == "log_fix":
            entry = payload.get("entry", "")
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
        return f"Current page state:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nThe screenshot is attached as an image. LOOK AT IT and describe what you see."

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
        return f"Observation after action:\n```json\n{json.dumps(context, indent=2)[:8000]}\n```\n\nFresh screenshot attached. LOOK AT IT and describe what changed."

    def _build_slim_observation(self, obs: dict) -> dict:
        """Build context-friendly slim observation. Port of build_slim_observation from main.py."""
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
            "failed_requests": fail_count,
            "hint": "Use search_network(query) to search requests",
            "recent": network_lines[-20:] if network_lines else [],
        }

        return {
            "dom": dom_summary,
            "console": console_summary,
            "network": network_summary,
            "url": obs.get("url", ""),
        }

    def _write_scratch_files(self, obs: dict):
        """Write full observation data to scratch files."""
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        os.makedirs(SCRATCH_NET_BODIES, exist_ok=True)

        with open(os.path.join(SCRATCH_DIR, "obs_dom.txt"), "w", encoding="utf-8") as f:
            f.write(obs.get("dom", ""))

        with open(os.path.join(SCRATCH_DIR, "obs_console.log"), "w", encoding="utf-8") as f:
            f.write(obs.get("console", ""))

        with open(os.path.join(SCRATCH_DIR, "obs_network.log"), "w", encoding="utf-8") as f:
            f.write(obs.get("network", ""))

        # Save screenshot
        if obs.get("screenshot_base64"):
            import base64
            with open(os.path.join(SCRATCH_DIR, "current_view.png"), "wb") as f:
                f.write(base64.b64decode(obs["screenshot_base64"]))

        # Extract network bodies
        network_raw = obs.get("network", "")
        for line in network_raw.split("\n"):
            if line.startswith("📦 DATA ["):
                try:
                    bracket_end = line.index("]")
                    url_key = line[len("📦 DATA ["):bracket_end]
                    body = line[bracket_end + 2:]
                    url_hash = hashlib.md5(url_key.encode()).hexdigest()[:12]
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', url_key)
                    body_path = os.path.join(SCRATCH_NET_BODIES, f"{safe_name}_{url_hash}.txt")
                    with open(body_path, "w", encoding="utf-8") as f:
                        f.write(body)
                except Exception:
                    pass

    def _record_fix_attempt(self, action_data: dict):
        """Track fix attempts."""
        attempt = {
            "attempt_number": len(self.fix_attempts) + 1,
            "action": action_data.get("action"),
            "thought": action_data.get("thought", "")[:300],
            "timestamp": int(time.time()),
        }
        payload = action_data.get("payload", {})
        if action_data.get("action") == "inject_js":
            attempt["code_preview"] = payload.get("code", "")[:200]
        elif action_data.get("action") == "inject_css":
            attempt["css_preview"] = payload.get("css", "")[:200]
        self.fix_attempts.append(attempt)

    def _trim_history(self):
        """Keep only recent messages to prevent context overflow."""
        max_messages = self.config.max_history * 2
        if len(self.messages) > max_messages:
            # Keep the first message (initial context) and the last N
            self.messages = [self.messages[0]] + self.messages[-(max_messages - 1):]
```

---

## PHASE 6: Main Entry Point

### Step 6.1: Create `main.py`

```python
"""TST2SK — Troubleshooting Tier 2 Sidekick. Entry point."""
import asyncio
import sys
from rich.console import Console
from rich.prompt import Prompt, IntPrompt
from rich.panel import Panel

from config import AppConfig, PROVIDER_PRESETS
from engine.brain import Brain

console = Console()

def setup_config() -> AppConfig:
    """Interactive config wizard."""
    config = AppConfig.from_env()

    # If env vars are set, use them
    if config.api_key and config.base_url and config.model:
        console.print(f"[green]Config loaded from .env[/green]")
        console.print(f"  Provider: {config.base_url}")
        console.print(f"  Model: {config.model}")
        use_env = Prompt.ask("Use this config?", choices=["y", "n"], default="y")
        if use_env == "y":
            return config

    # Interactive setup
    console.print(Panel("⚙️  TST2SK Configuration", style="bold blue"))

    providers = list(PROVIDER_PRESETS.keys())
    console.print("Available providers:")
    for i, p in enumerate(providers, 1):
        console.print(f"  {i}. {p}")
    choice = IntPrompt.ask("Select provider", default=1)
    provider_name = providers[min(choice - 1, len(providers) - 1)]
    preset = PROVIDER_PRESETS[provider_name]

    if provider_name == "custom":
        config.base_url = Prompt.ask("Enter API base URL")
        config.model = Prompt.ask("Enter model name")
    else:
        config.base_url = preset["base_url"]
        if preset["models"]:
            console.print("Available models:")
            for i, m in enumerate(preset["models"], 1):
                console.print(f"  {i}. {m}")
            model_choice = IntPrompt.ask("Select model", default=1)
            config.model = preset["models"][min(model_choice - 1, len(preset["models"]) - 1)]
        else:
            config.model = Prompt.ask("Enter model name")

    config.api_key = Prompt.ask("Enter API key", password=True)

    return config


async def main():
    console.print(Panel(
        "[bold]TST2SK[/bold] — Troubleshooting Tier 2 Sidekick\n"
        "Autonomous web page troubleshooter",
        style="bold cyan",
    ))

    config = setup_config()
    console.print()

    url = Prompt.ask("🌐 Enter the URL to troubleshoot")
    query = Prompt.ask("❓ What's the issue?")

    brain = Brain(config)
    try:
        await brain.start(url, query)
    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrupted by user[/yellow]")
    finally:
        await brain.browser.close()
        console.print("[dim]Browser closed. Session ended.[/dim]")


if __name__ == "__main__":
    # Install playwright browsers on first run
    try:
        asyncio.run(main())
    except Exception as e:
        console.print(f"[red]Fatal error: {e}[/red]")
        sys.exit(1)
```

---

## PHASE 7: Copy Existing Assets

### Step 7.1: Copy PLAYBOOKS.md

Copy the existing `playbooks/PLAYBOOKS.md` from the current TS Sidekick V2 project into `tst2sk/playbooks/PLAYBOOKS.md`. This file is read-only — the search_playbook function reads from it.

### Step 7.2: Copy fixes.log (if any)

If `kb/fixes.log` has entries from previous testing, copy it to `tst2sk/kb/fixes.log`. Otherwise the file will be created on first `log_fix` action.

---

## PHASE 8: First Run Setup

### Step 8.1: Install dependencies

```bash
cd tst2sk
pip install -r requirements.txt
playwright install chromium
```

### Step 8.2: Create .env file

```bash
cp .env.example .env
# Edit .env with your API key and preferences
```

### Step 8.3: Test run

```bash
python main.py
```

The app should:
1. Show the config wizard (or load from .env)
2. Ask for a URL and concern
3. Launch a visible Chromium window
4. Navigate to the URL
5. Start the agent loop — showing thoughts, actions, and results in the terminal
6. The AI investigates autonomously until it delivers a fix or hits max turns

---

## Key Differences from TS Sidekick V2

| Feature | V2 (old) | TST2SK (new) |
|---------|----------|--------------|
| Browser control | Chrome extension + WebSocket | Playwright + CDP directly |
| AI interaction | IDE reads/writes JSON files | OpenAI-compatible API call |
| Screenshot delivery | base64 in JSON file | base64 in API vision message |
| Console/network | Chrome debugger events via extension | Playwright page event listeners |
| JS injection (CSP bypass) | Extension uses chrome.debugger | Playwright CDP session Runtime.evaluate |
| User interface | IDE chat + sidepanel | Terminal (Rich) + visible browser |
| Configuration | Hardcoded in extension | .env + interactive wizard |
| Dependencies | Chrome extension + FastAPI + IDE | Just Python + Playwright |

## Critical Implementation Notes

1. **The DOM capture JavaScript in observer.py** is IDENTICAL to the extension's `captureObservation()` function. Do not change it — the brain protocol and diagnostic engine depend on the exact output format (★, ·, 📜, 🎨, [HIDDEN:...] markers).

2. **The diagnostic engine** (`cross_reference_diagnostics`) must be copied VERBATIM from main.py. It's pure Python that reads scratch files — no server dependencies.

3. **CDP for JS injection is critical.** Using `page.evaluate()` alone is subject to CSP. The `cdp.send("Runtime.evaluate", ...)` call bypasses CSP, which is essential for Shopify stores.

4. **Screenshot in every API call.** The screenshot_base64 is sent as a vision image in every AI request. This is the equivalent of screenshot_inline in brain_input.json. The AI model MUST be vision-capable.

5. **Network body capture.** Playwright's `response.text()` can fail on binary responses. Always wrap in try/except. Only capture bodies for "interesting" URLs (containing /api/, .json, or cart).

6. **History trimming.** Keep only the last 20 exchanges. The first message (initial context) is always preserved. Without trimming, the context window fills up fast with observation data.

7. **Console log format.** Playwright's console listener gives us `msg.type` and `msg.text`. Format as `[type] text` to match the existing format the diagnostic engine expects.

8. **The playbooks and KB functions** are pure file-reading Python. They have zero dependencies on the server, extension, or any framework. Just copy them.

9. **No async_playwright import at top level.** Import inside the launch() method or use `from playwright.async_api import async_playwright`. The import must happen after `playwright install chromium` has been run.

10. **For Google AI Studio specifically:** The base URL is `https://generativelanguage.googleapis.com/v1beta/openai/` and the API key goes in the Authorization header as a Bearer token. The openai SDK handles this automatically when you set api_key and base_url.
