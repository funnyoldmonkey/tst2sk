"""Execute AI actions in the browser."""
import os
import json as _json_mod
import base64
import io
from PIL import Image
from browser.controller import BrowserController

SCRATCH_DIR = "scratch"


def _save_cdp_result(filename: str, data) -> str:
    """Save CDP result to scratch file. Returns the file path."""
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    filepath = os.path.join(SCRATCH_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            _json_mod.dump(data, f, indent=2, default=str)
    return filepath

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
            # Dispatch input/change events for React/Angular/Vue compatibility
            await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (el) {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", selector)
            return f"Typed into: {selector}"

        elif action == "scroll":
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            # Parameterized evaluation — prevents JS injection via payload
            await page.evaluate("([x, y]) => window.scrollBy(x, y)", [x, y])
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
            # Delegate to controller.navigate() — handles wait strategy + CDP reconnection
            await browser.navigate(url)
            return f"Navigated to: {url}"

        elif action == "inject_js":
            code = payload.get("code", "")
            # Use CDP Runtime.evaluate to bypass CSP
            result = await cdp.send("Runtime.evaluate", {
                "expression": code,
                "userGesture": True,
                "awaitPromise": True,
                "returnByValue": True,
            })
            if "exceptionDetails" in result:
                err = result["exceptionDetails"].get("exception", {}).get("description", "Unknown error")
                browser.console_logs.append(f"[error] Script exception: {err}")
                return f"[error] inject_js exception: {err}"
            # Extract return value if present
            ret_val = result.get("result", {}).get("value")
            if ret_val is not None:
                preview = str(ret_val)[:2000]
                return f"inject_js executed successfully. Return value: {preview}"
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
                    text: (el.innerText || '').trim().substring(0, 200),
                    rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
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
                    }, {}),
                    disabled: el.disabled || false,
                    href: el.href || null,
                };
            }""", selector)
            if result == "NOT_FOUND":
                return f"[error] inspect_element: selector not found — \"{selector}\""
            # Return the actual data directly to the AI (not just "logged to console")
            result_str = _json_mod.dumps(result, indent=1, default=str)[:4000]
            browser.console_logs.append(f">>> INSPECT [{selector}]: {result_str[:500]}")
            return f"inspect_element({selector}):\n{result_str}"

        elif action == "run_test":
            code = payload.get("code", "")
            # Wrapper captures BOTH success/fail AND any return value from the code.
            # If code returns data (e.g., DOM queries), it comes back in "data" field.
            # If code throws, error message comes back in "message" field.
            wrapped = f"""(function(){{
                try {{
                    const __result = (function(){{ {code} }})();
                    if (__result !== undefined) {{
                        // Code returned data — include it
                        const preview = typeof __result === 'string' ? __result.substring(0, 3000)
                            : JSON.stringify(__result, null, 1).substring(0, 3000);
                        return {{ success: true, message: 'Test Passed', data: preview }};
                    }}
                    return {{ success: true, message: 'Test Passed' }};
                }} catch(e) {{
                    return {{ success: false, message: e.message }};
                }}
            }})()"""
            # 10-second timeout prevents infinite loops in test code
            import asyncio as _asyncio
            try:
                result = await _asyncio.wait_for(page.evaluate(wrapped), timeout=10.0)
            except _asyncio.TimeoutError:
                result = {"success": False, "message": "Test timed out after 10 seconds"}
            browser.console_logs.append(f">>> TEST_RESULT: {result}")
            return f"run_test: {result}"

        elif action == "observe":
            return "observe requested — fresh observation coming"

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
            x = max(0, int(result["x"] * dpr))
            y = max(0, int(result["y"] * dpr))
            x2 = min(img.width, x + int(result["w"] * dpr))
            y2 = min(img.height, y + int(result["h"] * dpr))
            if x2 > x and y2 > y:
                cropped = img.crop((x, y, x2, y2))
                crop_path = "scratch/element_capture.png"
                cropped.save(crop_path, "PNG")
                browser.console_logs.append(
                    f"🔍 ELEMENT CAPTURED: {selector} at {result['x']},{result['y']} "
                    f"({result['w']}×{result['h']}) — cropped to scratch/element_capture.png"
                )
                return (
                    f"capture_element({selector}): saved to scratch/element_capture.png — "
                    f"position ({int(result['x'])},{int(result['y'])}), "
                    f"size {int(result['w'])}×{int(result['h'])}px"
                )
            return f"capture_element({selector}): element has zero dimensions — nothing to capture"

        elif action == "click_at_position":
            x = payload["x"]
            y = payload["y"]
            await page.mouse.click(x, y)
            return f"Clicked at position ({x}, {y})"

        elif action == "post_message" or action == "answer_user":
            message = payload.get("message", payload.get("text", ""))
            return f"MESSAGE_TO_USER: {message}"

        elif action == "log_fix":
            return "log_fix handled by engine"

        # ─── CDP Direct Actions (Tier 1) ─────────────────────────
        elif action == "cdp_get_dom_tree":
            depth = int(payload.get("depth", 3))
            result = await cdp.send("DOM.getDocument", {"depth": min(depth, 6)})
            root = result.get("root", {})
            # Compact the tree to avoid massive output
            def _compact_node(node, max_depth=3, current=0):
                if current >= max_depth:
                    child_count = len(node.get("children", []))
                    return {"tag": node.get("nodeName", "?"), "childCount": child_count} if child_count else {"tag": node.get("nodeName", "?")}
                compact = {
                    "tag": node.get("nodeName", "?"),
                    "id": node.get("attributes", {}).get("id") if isinstance(node.get("attributes"), dict) else "",
                }
                attrs = node.get("attributes", [])
                if isinstance(attrs, list):
                    for i in range(0, len(attrs) - 1, 2):
                        if attrs[i] == "id":
                            compact["id"] = attrs[i + 1]
                        elif attrs[i] == "class":
                            compact["class"] = attrs[i + 1][:60]
                children = node.get("children", [])
                if children:
                    compact["children"] = [_compact_node(c, max_depth, current + 1) for c in children[:20]]
                return compact
            tree = _compact_node(root, min(depth, 6))
            full_str = _json_mod.dumps(tree, indent=1)
            saved = _save_cdp_result("cdp_dom_tree.json", tree)
            preview = full_str[:4000]
            truncated = len(full_str) > 4000
            browser.console_logs.append(f">>> CDP DOM tree (depth={depth}): {len(full_str)} chars → saved to {saved}")
            msg = f"cdp_get_dom_tree (depth={depth}, {len(full_str)} chars):\n{preview}"
            if truncated:
                msg += f"\n... [TRUNCATED — full tree saved to {saved}, use read_network_body or search_dom to explore]"
            return msg

        elif action == "cdp_get_cookies":
            result = await cdp.send("Network.getCookies")
            cookies = result.get("cookies", [])
            # Full data saved, summary returned
            _save_cdp_result("cdp_cookies.json", cookies)
            summary = []
            for c in cookies:
                val_preview = str(c.get("value", ""))[:30]
                summary.append({
                    "name": c.get("name", "?"),
                    "domain": c.get("domain", "?"),
                    "value": val_preview + ("..." if len(str(c.get("value", ""))) > 30 else ""),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                })
            cookies_str = _json_mod.dumps(summary, indent=1)
            browser.console_logs.append(f">>> CDP cookies: {len(cookies)} total → saved to scratch/cdp_cookies.json")
            return f"cdp_get_cookies ({len(cookies)} total, full data in scratch/cdp_cookies.json):\n{cookies_str[:4000]}"

        elif action == "cdp_get_computed_style":
            selector = payload.get("selector", "")
            if not selector:
                return "[error] cdp_get_computed_style requires a 'selector' payload"
            doc_result = await cdp.send("DOM.getDocument", {"depth": 0})
            root_id = doc_result["root"]["nodeId"]
            search_result = await cdp.send("DOM.querySelector", {
                "nodeId": root_id,
                "selector": selector,
            })
            node_id = search_result.get("nodeId", 0)
            if not node_id:
                return f"[error] cdp_get_computed_style: selector not found — \"{selector}\""
            style_result = await cdp.send("CSS.getComputedStyleForNode", {
                "nodeId": node_id,
            })
            computed = style_result.get("computedStyle", [])
            # Save ALL computed styles to scratch
            all_styles = {p["name"]: p["value"] for p in computed}
            _save_cdp_result("cdp_computed_style.json", {"selector": selector, "all_properties": all_styles})
            # Return filtered useful properties in message
            useful_props = {
                "display", "visibility", "opacity", "position", "z-index",
                "width", "height", "max-height", "max-width", "overflow",
                "color", "background-color", "font-size", "font-weight",
                "pointer-events", "cursor", "clip-path", "transform",
                "margin-top", "margin-bottom", "padding-top", "padding-bottom",
                "border", "box-shadow", "text-decoration", "float", "flex",
            }
            filtered = {p["name"]: p["value"] for p in computed if p["name"] in useful_props}
            style_str = _json_mod.dumps(filtered, indent=1)
            browser.console_logs.append(f">>> CDP computed style [{selector}]: {len(all_styles)} total props, {len(filtered)} shown → full in scratch/cdp_computed_style.json")
            return f"cdp_get_computed_style({selector}, {len(filtered)} key props, {len(all_styles)} total in scratch/cdp_computed_style.json):\n{style_str}"

        elif action == "cdp_get_page_metrics":
            metrics = await cdp.send("Performance.getMetrics")
            metric_list = metrics.get("metrics", [])
            all_metrics = {m["name"]: round(m["value"], 2) for m in metric_list}
            _save_cdp_result("cdp_page_metrics.json", all_metrics)
            useful = {"Timestamp", "Documents", "Frames", "JSEventListeners",
                       "Nodes", "LayoutCount", "RecalcStyleCount", "LayoutDuration",
                       "RecalcStyleDuration", "ScriptDuration", "TaskDuration",
                       "JSHeapUsedSize", "JSHeapTotalSize"}
            filtered = {k: v for k, v in all_metrics.items() if k in useful}
            metrics_str = _json_mod.dumps(filtered, indent=1)
            browser.console_logs.append(f">>> CDP page metrics: {len(all_metrics)} total, {len(filtered)} shown → full in scratch/cdp_page_metrics.json")
            return f"cdp_get_page_metrics ({len(filtered)} key metrics, {len(all_metrics)} total in scratch/cdp_page_metrics.json):\n{metrics_str}"

        elif action == "cdp_query_selector_all":
            selector = payload.get("selector", "")
            if not selector:
                return "[error] cdp_query_selector_all requires a 'selector' payload"
            # Parameterized evaluation — selector passed as argument, not concatenated into JS
            js_code = """((sel) => {
                const els = document.querySelectorAll(sel);
                return Array.from(els).slice(0, 50).map((el, i) => {
                    const rect = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    return {
                        index: i,
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        classes: (el.className && typeof el.className === 'string') ? el.className.substring(0, 60) : '',
                        text: (el.innerText || el.value || '').trim().substring(0, 80),
                        visible: cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05,
                        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                        disabled: el.disabled || false,
                    };
                });
            })"""
            # Use page.evaluate with parameterized argument to prevent injection
            result_value = await page.evaluate(js_code, selector)
            # Wrap in same format as CDP Runtime.evaluate for consistency
            result = {"result": {"value": result_value}}
            value = result.get("result", {}).get("value", [])
            # Save full results (up to 50), return first 20 in message
            _save_cdp_result("cdp_query_results.json", {"selector": selector, "total": len(value), "elements": value})
            preview = value[:20]
            result_str = _json_mod.dumps(preview, indent=1)[:4000]
            browser.console_logs.append(f">>> CDP querySelectorAll({selector}): {len(value)} results → saved to scratch/cdp_query_results.json")
            msg = f"cdp_query_selector_all({selector}, {len(value)} matches):\n{result_str}"
            if len(value) > 20:
                msg += f"\n... [showing 20 of {len(value)} — full results in scratch/cdp_query_results.json]"
            return msg

        else:
            return f"[error] Unknown action: {action}"

    except Exception as e:
        return f"[error] Action {action} failed: {str(e)}"
