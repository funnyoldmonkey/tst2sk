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
