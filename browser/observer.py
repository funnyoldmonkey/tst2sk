"""Page observation — captures DOM, screenshot, console, network."""
import base64
from browser.controller import BrowserController

async def capture_observation(browser: BrowserController) -> dict:
    """Capture the full page state. Returns a dict with dom, console, network, screenshot_base64, url.

    Robust against browser crashes, navigation, and destroyed execution contexts.
    Returns partial data if some captures fail rather than crashing the session.
    """
    page = browser.page
    dom_text = ""
    screenshot_base64 = ""
    url = ""

    # 1. Capture DOM — same logic as the extension's describe() function
    try:
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
                        else if (parseFloat(cs.opacity) < 0.05) visFlag = ' [HIDDEN:opacity]';
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
    except Exception as e:
        dom_text = f"[DOM capture error: {e}]"

    # 2. Screenshot as base64
    try:
        screenshot_bytes = await page.screenshot(full_page=False, type="png")
        screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    except Exception as e:
        screenshot_base64 = ""

    # 3. Snapshot diagnostics (console + network) and clear for next turn
    try:
        console_logs, network_log, _ = browser.clear_diagnostics()
    except Exception:
        console_logs, network_log = [], []

    # 4. Current URL
    try:
        url = page.url
    except Exception:
        url = "unknown"

    return {
        "dom": dom_text,
        "console": "\n".join(console_logs) if console_logs else "No console logs.",
        "network": "\n".join(network_log) if network_log else "No network activity.",
        "screenshot_base64": screenshot_base64,
        "url": url,
    }
