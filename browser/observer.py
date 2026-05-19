"""Page observation — captures DOM, screenshot, console, network, visibility analysis."""
import base64
from browser.controller import BrowserController

async def capture_observation(browser: BrowserController) -> dict:
    """Capture the full page state. Returns a dict with dom, console, network, screenshot_base64, url, visibility_issues.

    Robust against browser crashes, navigation, and destroyed execution contexts.
    Returns partial data if some captures fail rather than crashing the session.
    """
    page = browser.page
    dom_text = ""
    screenshot_base64 = ""
    url = ""
    visibility_issues = []

    # 0. Quick readiness gate — wait up to 3s for document.readyState === 'complete'
    #    This catches mid-navigation captures (e.g., after click triggers a page transition)
    try:
        await page.wait_for_function(
            "document.readyState === 'complete'",
            timeout=3000,
        )
    except Exception:
        pass  # Timeout or navigation — capture what we have

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
            // Collect elements including shadow DOM children (1 level deep)
            const allEls = Array.from(document.querySelectorAll('*'));
            const shadowEls = [];
            for (const el of allEls) {
                if (el.shadowRoot) {
                    const shadowChildren = el.shadowRoot.querySelectorAll('*');
                    for (const sc of shadowChildren) {
                        shadowEls.push(sc);
                    }
                }
            }
            return allEls.concat(shadowEls)
                .map(describe)
                .filter(x => x !== null)
                .join('\\n');
        }""")
    except Exception as e:
        dom_text = f"[DOM capture error: {e}]"

    # 1b. Deep visibility analysis — trace WHY hidden interactive elements are hidden
    try:
        visibility_issues = await page.evaluate("""() => {
            const issues = [];
            const interactives = document.querySelectorAll('button, a, input, select, textarea, [role="button"]');
            let checked = 0;
            for (const el of interactives) {
                if (checked >= 20) break;  // Cap at 20 to avoid performance hit
                const cs = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const isHidden = (
                    cs.display === 'none' ||
                    cs.visibility === 'hidden' ||
                    parseFloat(cs.opacity) < 0.05 ||
                    (rect.width === 0 && rect.height === 0)
                );
                if (!isHidden) continue;
                checked++;

                const text = (el.innerText || el.value || el.title || el.getAttribute('aria-label') || '').trim().substring(0, 50);
                const tag = el.tagName.toLowerCase();
                const id = el.id ? '#' + el.id : '';
                const cls = el.className && typeof el.className === 'string'
                    ? '.' + el.className.split(/\\s+/).slice(0, 3).join('.') : '';
                const selector = tag + id + cls;

                // Trace the cause
                const causes = [];

                // Self styles
                if (cs.display === 'none') causes.push('self: display:none');
                if (cs.visibility === 'hidden') causes.push('self: visibility:hidden');
                if (parseFloat(cs.opacity) < 0.05) causes.push('self: opacity:' + cs.opacity);
                if (rect.width === 0 && rect.height === 0) causes.push('self: zero dimensions');

                // Check parent chain (up to 5 levels)
                let parent = el.parentElement;
                let depth = 0;
                while (parent && depth < 5) {
                    depth++;
                    const pcs = window.getComputedStyle(parent);
                    const ptag = parent.tagName.toLowerCase();
                    const pid = parent.id ? '#' + parent.id : '';

                    if (pcs.display === 'none') {
                        causes.push('parent(' + ptag + pid + '): display:none');
                        break;  // No need to go further — this is the blocker
                    }
                    if (pcs.visibility === 'hidden') {
                        causes.push('parent(' + ptag + pid + '): visibility:hidden');
                    }
                    if (parseFloat(pcs.opacity) < 0.05) {
                        causes.push('parent(' + ptag + pid + '): opacity:' + pcs.opacity);
                    }
                    if (pcs.overflow === 'hidden') {
                        const prect = parent.getBoundingClientRect();
                        if (prect.height < 5 || prect.width < 5) {
                            causes.push('parent(' + ptag + pid + '): overflow:hidden + tiny container (' + Math.round(prect.width) + 'x' + Math.round(prect.height) + ')');
                        }
                    }
                    // Check z-index stacking
                    if (pcs.position !== 'static' && parseInt(pcs.zIndex) < 0) {
                        causes.push('parent(' + ptag + pid + '): negative z-index:' + pcs.zIndex);
                    }
                    parent = parent.parentElement;
                }

                // Check if disabled
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
                    causes.push('disabled attribute');
                }

                if (causes.length > 0) {
                    issues.push({
                        element: selector,
                        text: text || '(no text)',
                        causes: causes,
                    });
                }
            }
            return issues;
        }""")
    except Exception:
        visibility_issues = []

    # 1c. Capture window.Shopify object (if present) — critical for Shopify debugging
    shopify_data = None
    try:
        shopify_data = await page.evaluate("""() => {
            if (!window.Shopify) return null;
            const s = window.Shopify;
            const data = {
                shop: s.shop || null,
                theme: s.theme ? { name: s.theme.name, id: s.theme.id, role: s.theme.role } : null,
                locale: s.locale || null,
                currency: s.currency ? { active: s.currency.active, rate: s.currency.rate } : null,
                routes: s.routes || null,
            };
            // Capture product/variant info if on a product page
            if (typeof meta !== 'undefined' && meta.product) {
                data.product_meta = {
                    id: meta.product.id,
                    type: meta.product.type,
                };
                if (meta.product.variants) {
                    data.product_meta.variant_count = meta.product.variants.length;
                }
            }
            // Capture selected variant
            if (typeof meta !== 'undefined' && meta.selectedVariantId) {
                data.selected_variant_id = meta.selectedVariantId;
            }
            // Cart token
            if (s.cart) {
                data.cart_token = typeof s.cart === 'object' ? s.cart.token : null;
            }
            return data;
        }""")
    except Exception:
        shopify_data = None

    # 1d. Interactive element inventory — structured scan for AI actionability
    #     Groups elements by type (buttons, links, inputs, selects) with text, selector,
    #     visibility, disabled state, and bounding rect. This gives the AI a complete
    #     picture of the page's interactive surface from turn 1.
    interactive_inventory = None
    try:
        interactive_inventory = await page.evaluate("""() => {
            const inventory = { buttons: [], links: [], inputs: [], selects: [], forms: [] };
            const seen = new Set();  // Deduplicate by text+tag

            function bestSelector(el) {
                // Build the most reliable CSS selector for this element
                if (el.id) return el.tagName.toLowerCase() + '#' + el.id;
                const dti = el.getAttribute('data-testid');
                if (dti) return el.tagName.toLowerCase() + '[data-testid="' + dti + '"]';
                const name = el.getAttribute('name');
                if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
                const ariaLabel = el.getAttribute('aria-label');
                if (ariaLabel) return el.tagName.toLowerCase() + '[aria-label="' + ariaLabel.replace(/"/g, '\\\\"') + '"]';
                // Class-based fallback (first 3 non-utility classes)
                const cls = el.className && typeof el.className === 'string'
                    ? el.className.split(/\\s+/).filter(c => c && !c.includes(':') && c.length < 30).slice(0, 3).join('.')
                    : '';
                if (cls) return el.tagName.toLowerCase() + '.' + cls;
                return el.tagName.toLowerCase();
            }

            function elData(el) {
                const cs = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const text = (el.innerText || el.value || el.title || el.getAttribute('aria-label') || '').trim().substring(0, 80);
                const isHidden = cs.display === 'none' || cs.visibility === 'hidden' ||
                    parseFloat(cs.opacity) < 0.05 || (rect.width === 0 && rect.height === 0);
                return {
                    text: text || '(no text)',
                    selector: bestSelector(el),
                    visible: !isHidden,
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                    rect: isHidden ? null : { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
                };
            }

            // Buttons (including role="button")
            const buttons = document.querySelectorAll('button, [role="button"]');
            for (const el of buttons) {
                const d = elData(el);
                const key = d.text + '|' + el.tagName;
                if (seen.has(key) && !d.visible) continue;  // Skip hidden duplicates
                seen.add(key);
                if (inventory.buttons.length < 60) inventory.buttons.push(d);
            }

            // Links (only with href or onclick)
            const links = document.querySelectorAll('a[href], a[onclick]');
            for (const el of links) {
                const d = elData(el);
                d.href = (el.getAttribute('href') || '').substring(0, 100);
                const key = d.text + '|a';
                if (seen.has(key) && !d.visible) continue;
                seen.add(key);
                if (inventory.links.length < 40) inventory.links.push(d);
            }

            // Inputs
            const inputs = document.querySelectorAll('input, textarea');
            for (const el of inputs) {
                const d = elData(el);
                d.type = el.type || 'text';
                if (inventory.inputs.length < 20) inventory.inputs.push(d);
            }

            // Selects
            const selects = document.querySelectorAll('select');
            for (const el of selects) {
                const d = elData(el);
                d.options = Array.from(el.options).slice(0, 10).map(o => o.text.trim().substring(0, 40));
                if (inventory.selects.length < 10) inventory.selects.push(d);
            }

            // Forms
            const forms = document.querySelectorAll('form');
            for (const el of forms) {
                const d = {
                    action: (el.action || '').substring(0, 100),
                    method: el.method || 'get',
                    selector: bestSelector(el),
                    inputCount: el.querySelectorAll('input, select, textarea').length,
                    hasSubmit: !!el.querySelector('button[type="submit"], input[type="submit"]'),
                };
                if (inventory.forms.length < 10) inventory.forms.push(d);
            }

            // Summary counts
            inventory.summary = {
                total_buttons: buttons.length,
                visible_buttons: inventory.buttons.filter(b => b.visible).length,
                total_links: links.length,
                visible_links: inventory.links.filter(l => l.visible).length,
                total_inputs: inputs.length,
                total_selects: selects.length,
                total_forms: forms.length,
            };

            return inventory;
        }""")
    except Exception:
        interactive_inventory = None

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
        "visibility_issues": visibility_issues,
        "shopify": shopify_data,  # None if not a Shopify site
        "interactive_inventory": interactive_inventory,  # Structured element scan
    }
