"""Browser lifecycle management using Playwright + CDP."""
import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, CDPSession

# Maximum number of network response bodies to keep in memory
MAX_NETWORK_BODIES = 200

class BrowserController:
    """Manages a Chromium browser instance with CDP access."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp: CDPSession | None = None
        self._launched = False  # Guard against double launch

        # Diagnostic storage — populated by event listeners
        self.console_logs: list[str] = []
        self.network_log: list[str] = []
        self.network_bodies: dict[str, str] = {}  # url -> response body (for interesting URLs)

    async def launch(self):
        """Launch browser, create page, attach CDP, start listeners."""
        if self._launched:
            return  # Already launched — prevent resource leak from double launch
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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

        self._launched = True

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
                    # Cap network bodies to prevent unbounded memory growth
                    if len(self.network_bodies) >= MAX_NETWORK_BODIES:
                        # Evict oldest entry
                        oldest_key = next(iter(self.network_bodies))
                        del self.network_bodies[oldest_key]
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
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def clear_diagnostics(self):
        """Snapshot and clear diagnostics for the current turn."""
        logs = list(self.console_logs)
        network = list(self.network_log)
        bodies = dict(self.network_bodies)
        self.console_logs.clear()
        self.network_log.clear()
        # Don't clear network_bodies — they accumulate for read_network_body lookups
        return logs, network, bodies

    async def close(self):
        """Clean shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._launched = False
