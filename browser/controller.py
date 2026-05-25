"""Browser lifecycle management using Playwright + CDP."""
import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, CDPSession

# Maximum number of network response bodies to keep in memory
MAX_NETWORK_BODIES = 200
# Maximum console/network log lines to prevent unbounded memory growth
MAX_LOG_LINES = 500

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
        self.network_details: dict[str, dict] = {}  # url -> detailed request/response info

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
        await self.cdp.send("DOM.enable")       # Must come before CSS.enable
        await self.cdp.send("CSS.enable")        # Requires DOM.enable first
        await self.cdp.send("Performance.enable")

        # --- Console log listener ---
        self.page.on("console", self._on_console)

        # --- Network listeners ---
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)

        self._launched = True

    def _on_console(self, msg):
        """Capture console messages (capped at MAX_LOG_LINES)."""
        level = msg.type  # 'log', 'error', 'warning', 'info', etc.
        text = msg.text
        self.console_logs.append(f"[{level}] {text}")
        # Evict oldest entries if over cap
        if len(self.console_logs) > MAX_LOG_LINES:
            self.console_logs = self.console_logs[-MAX_LOG_LINES:]

    async def _on_response(self, response):
        """Capture network responses (capped at MAX_LOG_LINES)."""
        url = response.url
        status = response.status

        # Build request/response details dict
        request = response.request
        details = {
            "url": url,
            "method": request.method,
            "request_headers": request.headers,
            "request_body": request.post_data,
            "status": status,
            "response_headers": response.headers,
            "response_body": None
        }

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
                    # Cap individual body size to prevent memory exhaustion (1MB max)
                    if len(body) > 1_000_000:
                        body = body[:1_000_000] + "\n... [TRUNCATED — body exceeded 1MB]"
                    # Cap network bodies count to prevent unbounded memory growth
                    if len(self.network_bodies) >= MAX_NETWORK_BODIES:
                        oldest_key = next(iter(self.network_bodies))
                        del self.network_bodies[oldest_key]
                    self.network_bodies[url] = body
                except Exception:
                    pass
            else:
                self.network_log.append(f"✅ SUCCESS: {url.split('?')[0]} ({status})")

        # Fetch body for interesting requests or failures status >= 400
        is_interesting_details = (
            status >= 400 or "/api/" in url or ".json" in url or "cart" in url
        )
        if is_interesting_details:
            try:
                body = None
                if url in self.network_bodies:
                    body = self.network_bodies[url]
                else:
                    body = await response.text()
                    if len(body) > 1_000_000:
                        body = body[:1_000_000] + "\n... [TRUNCATED — body exceeded 1MB]"
                details["response_body"] = body
            except Exception:
                pass

        # Save details
        if len(self.network_details) >= MAX_NETWORK_BODIES:
            oldest_key = next(iter(self.network_details))
            del self.network_details[oldest_key]
        self.network_details[url] = details

        # Cap network log (covers both success and failure paths)
        if len(self.network_log) > MAX_LOG_LINES:
            self.network_log = self.network_log[-MAX_LOG_LINES:]

    def _on_request_failed(self, request):
        """Capture failed network requests."""
        url = request.url
        failure = request.failure
        self.network_log.append(f"🚨 FAILED: {url} (network error: {failure})")
        # Cap network log
        if len(self.network_log) > MAX_LOG_LINES:
            self.network_log = self.network_log[-MAX_LOG_LINES:]

    async def _ensure_cdp_domains(self):
        """Re-establish CDP session and enable required domains after navigation.

        Navigation can destroy the CDP session. This re-attaches and enables
        all domains the agent relies on (Network, Runtime, DOM, CSS, Performance).
        """
        try:
            # Detach old session gracefully
            if self.cdp:
                try:
                    await self.cdp.detach()
                except Exception:
                    pass  # Already detached or dead — that's fine
                self.cdp = None  # Clear stale reference before attempting reconnection
            self.cdp = await self.page.context.new_cdp_session(self.page)
            await self.cdp.send("Network.enable")
            await self.cdp.send("Runtime.enable")
            await self.cdp.send("DOM.enable")
            await self.cdp.send("CSS.enable")
            await self.cdp.send("Performance.enable")
        except Exception:
            # CDP re-attach failed — self.cdp may be None, actions will get clear errors
            if self.cdp is None:
                pass  # Already cleared — actions will see NoneType errors and report them
            else:
                # Partial setup — session exists but some domains may be dead
                pass

    async def navigate(self, url: str):
        """Navigate to a URL and wait for full page load.

        Uses 'networkidle' (no requests for 500ms) to ensure SPAs have
        finished hydrating. Falls back to 'load' if networkidle times out
        (some sites have persistent polling connections).
        Re-establishes CDP session after navigation completes.
        """
        self.console_logs.clear()
        self.network_log.clear()
        self.network_bodies.clear()
        self.network_details.clear()
        try:
            await self.page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            # networkidle can timeout on sites with persistent connections (websockets, polling).
            # Fall back to 'load' which waits for the load event — still much better than domcontentloaded.
            try:
                await self.page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass  # Page is at least partially loaded — continue with what we have

        # Re-establish CDP session — navigation can destroy the old one
        await self._ensure_cdp_domains()

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
