"""Cross-reference diagnostic engine — auto-detects scenario type + classifies errors."""
import os
import re
from urllib.parse import urlparse

SCRATCH_DIR = "scratch"

# Known legitimate script sources (partial matches OK)
_KNOWN_SCRIPT_SOURCES = {
    "shopify", "cdn.shopify", "jquery", "react", "webpack",
    "google", "gtm", "gtag", "analytics", "facebook", "fbevents",
    "hotjar", "sentry", "bugsnag", "stripe", "paypal",
    "klaviyo", "mailchimp", "intercom", "zendesk", "drift",
    "cloudflare", "unpkg", "cdnjs", "jsdelivr",
}


def _classify_console_errors(console_content: str, dom_content: str) -> list[dict]:
    """Classify console errors as real, suspicious, or noise.

    Returns a list of dicts: {message, classification, reason}
    Classifications:
      - "real": JS exception or error from a known script in the DOM
      - "suspicious": References a script/module not found in DOM, or looks planted
      - "noise": Debug logs, warnings, or benign messages
    """
    scripts_in_dom = set(re.findall(r'📜 \[script\] src="([^"]+)"', dom_content))
    # Extract script filenames for quick lookup
    script_names = set()
    for src in scripts_in_dom:
        # Get filename: "/apps/stocksync/tracker.js" → "stocksync", "tracker"
        parts = src.lower().replace("\\", "/").split("/")
        for part in parts:
            clean = part.split(".")[0].split("?")[0]
            if clean and len(clean) > 2:
                script_names.add(clean)

    classified = []
    for line in console_content.split("\n"):
        line = line.strip()
        if not line:
            continue

        is_error = "[error]" in line.lower() or "🚨" in line
        is_warning = "[warning]" in line.lower() or "[warn]" in line.lower()

        if not is_error and not is_warning:
            continue  # Skip non-error lines

        if is_warning and not is_error:
            classified.append({
                "message": line[:200],
                "classification": "noise",
                "reason": "Warning, not error",
            })
            continue

        # Check if error references a known script source
        line_lower = line.lower()

        # Check for references to scripts NOT in the DOM — suspicious
        # Look for patterns like "StockSync", "AppName", module names
        name_refs = re.findall(r'(?:from |in |at |module )[\"\']?([A-Za-z][\w.-]+)', line)
        suspicious_refs = []
        for ref in name_refs:
            ref_lower = ref.lower().split(".")[0]
            # If it references something not in DOM scripts and not a known source
            if (ref_lower not in script_names
                    and not any(k in ref_lower for k in _KNOWN_SCRIPT_SOURCES)
                    and len(ref_lower) > 3):
                suspicious_refs.append(ref)

        if suspicious_refs:
            classified.append({
                "message": line[:200],
                "classification": "suspicious",
                "reason": f"References '{suspicious_refs[0]}' — no matching script found in DOM. Possibly planted.",
            })
        elif "typeerror" in line_lower or "referenceerror" in line_lower or "syntaxerror" in line_lower:
            classified.append({
                "message": line[:200],
                "classification": "real",
                "reason": "JS exception — likely real error",
            })
        elif "failed to load" in line_lower or "404" in line_lower or "cors" in line_lower:
            classified.append({
                "message": line[:200],
                "classification": "real",
                "reason": "Resource loading error — likely real",
            })
        elif any(k in line_lower for k in ["console.error", "uncaught", "unhandled"]):
            classified.append({
                "message": line[:200],
                "classification": "real",
                "reason": "Uncaught exception — real error",
            })
        else:
            # Default: if it's [error] but no clear JS exception pattern, flag as needs-verification
            classified.append({
                "message": line[:200],
                "classification": "suspicious",
                "reason": "Error logged but no JS exception pattern — verify with DOM evidence before trusting",
            })

    return classified


def _analyze_shopify(dom_content: str, network_content: str, scripts: list[str]) -> dict:
    """Deep Shopify-specific analysis — theme, apps, product page structure, hints."""
    analysis = {
        "theme": "unknown",
        "app_scripts": [],
        "page_type": "unknown",
        "product_hints": [],
        "cart_hints": [],
    }

    # --- Theme detection ---
    dom_lower = dom_content.lower()
    theme_patterns = {
        "Dawn": "dawn" in dom_lower and "shopify" in dom_lower,
        "Debut": "debut" in dom_lower,
        "Brooklyn": "brooklyn" in dom_lower,
        "Narrative": "narrative" in dom_lower,
        "Minimal": "minimal-theme" in dom_lower or "theme-minimal" in dom_lower,
        "Supply": "supply" in dom_lower and "theme" in dom_lower,
        "Prestige": "prestige" in dom_lower,
        "Impulse": "impulse" in dom_lower,
        "Turbo": "turbo" in dom_lower and "theme" in dom_lower,
    }
    for theme_name, detected in theme_patterns.items():
        if detected:
            analysis["theme"] = theme_name
            break

    # --- App script detection (3rd party Shopify apps) ---
    shopify_app_patterns = [
        (r'/apps/([^/\?"]+)', "Shopify App"),
        (r'bold-.*?\.js', "Bold App"),
        (r'recharge', "ReCharge Subscriptions"),
        (r'judgeme|judge\.me', "Judge.me Reviews"),
        (r'loox', "Loox Reviews"),
        (r'stamped', "Stamped.io Reviews"),
        (r'omnisend', "Omnisend"),
        (r'privy', "Privy"),
        (r'back-in-stock|backinstock', "Back In Stock"),
        (r'stocksync|stock-sync', "StockSync"),
    ]
    seen_apps = set()
    for script_src in scripts:
        for pattern, app_name in shopify_app_patterns:
            if re.search(pattern, script_src, re.IGNORECASE):
                if app_name not in seen_apps:
                    seen_apps.add(app_name)
                    analysis["app_scripts"].append({
                        "app": app_name,
                        "script": script_src.split("?")[0][-60:],
                    })

    # --- Page type detection ---
    if "product-form" in dom_lower or "product-template" in dom_lower or "data-product" in dom_lower:
        analysis["page_type"] = "product"
    elif "/collections" in dom_lower or "collection-template" in dom_lower:
        analysis["page_type"] = "collection"
    elif "/cart" in dom_lower or "cart-template" in dom_lower:
        analysis["page_type"] = "cart"
    elif "/checkout" in dom_lower:
        analysis["page_type"] = "checkout"

    # --- Product page hints ---
    if analysis["page_type"] == "product":
        hints = []
        # ATC button patterns
        if 'add to cart' in dom_lower or 'addtocart' in dom_lower:
            hints.append("ATC button present — search for: [type=\"submit\"], .product-form__submit, .btn--add-to-cart, [name=\"add\"]")
        else:
            hints.append("⚠️ No 'Add to Cart' text found — button may be hidden, renamed, or dynamically loaded")

        # Variant selectors
        if 'data-variant' in dom_lower or 'variant' in dom_lower:
            hints.append("Variant selectors detected — search for: [data-variant-id], .product-form__variant, input[name=\"id\"]")

        # Price elements
        if '.price' in dom_lower or 'data-price' in dom_lower:
            hints.append("Price elements found — search for: .price, [data-price], .product__price, .money")

        # Size selectors
        if 'size' in dom_lower:
            hints.append("Size options detected — search for: .size-selector, [data-option=\"Size\"], .swatch--size")

        analysis["product_hints"] = hints

    # --- Cart API hints ---
    net_lower = network_content.lower()
    if "/cart/add" in net_lower or "/cart.js" in net_lower:
        analysis["cart_hints"].append("Shopify Cart API active — check /cart/add.js, /cart/update.js, /cart/change.js responses")
    if "🚨 FAILED" in network_content and "/cart" in net_lower:
        analysis["cart_hints"].append("⚠️ Cart API request FAILED — check read_network_body for error details")

    return analysis


def cross_reference_diagnostics() -> dict:
    """
    Analyzes scratch/obs_dom.txt, scratch/obs_console.log, scratch/obs_network.log
    to detect the scenario and identify potential issues.
    """
    dom_path = os.path.join(SCRATCH_DIR, "obs_dom.txt")
    console_path = os.path.join(SCRATCH_DIR, "obs_console.log")
    network_path = os.path.join(SCRATCH_DIR, "obs_network.log")

    dom_content = ""
    if os.path.exists(dom_path):
        with open(dom_path, "r", encoding="utf-8") as f:
            dom_content = f.read()

    console_content = ""
    if os.path.exists(console_path):
        with open(console_path, "r", encoding="utf-8") as f:
            console_content = f.read()

    network_content = ""
    if os.path.exists(network_path):
        with open(network_path, "r", encoding="utf-8") as f:
            network_content = f.read()

    # --- Basic Extractions ---
    scripts = re.findall(r'📜 \[script\] src="([^"]+)"', dom_content)
    hidden_elements = re.findall(r'\[HIDDEN:.*?\]', dom_content)
    console_errors = [l for l in console_content.split("\n") if "[error]" in l.lower() or "🚨" in l]
    failed_requests = [l for l in network_content.split("\n") if "🚨 FAILED" in l]

    # --- Console Error Classification ---
    classified_errors = _classify_console_errors(console_content, dom_content)
    real_errors = [e for e in classified_errors if e["classification"] == "real"]
    suspicious_errors = [e for e in classified_errors if e["classification"] == "suspicious"]

    # --- Scenario Detection ---
    scenario = "generic_web_page"
    confidence = 0

    # Shopify detection (enhanced)
    shopify_signals = []
    if "shopify" in dom_content.lower():
        shopify_signals.append("DOM references Shopify")
    if "shopify" in network_content.lower():
        shopify_signals.append("Network requests to Shopify")
    if "cdn.shopify.com" in dom_content:
        shopify_signals.append("Shopify CDN assets loaded")
    if "myshopify.com" in network_content:
        shopify_signals.append("myshopify.com API calls")

    if shopify_signals:
        scenario = "shopify_store"
        confidence = min(95, 60 + len(shopify_signals) * 10)

    # Checkout/Cart detection
    if any(k in dom_content.lower() for k in ["cart", "checkout", "payment", "shipping"]):
        if scenario == "shopify_store":
            scenario = "shopify_checkout"
            confidence = min(95, confidence + 5)
        else:
            scenario = "ecommerce_checkout"
            confidence = 70

    # Auth/Login detection
    if any(k in dom_content.lower() for k in ["login", "sign-in", "password", "auth"]):
        if scenario == "generic_web_page":
            scenario = "auth_flow"
            confidence = 60

    # --- Shopify-Specific Analysis ---
    shopify_analysis = None
    if "shopify" in scenario:
        shopify_analysis = _analyze_shopify(dom_content, network_content, scripts)

    # --- Potential Issues ---
    issues = []
    if hidden_elements:
        issues.append(f"Found {len(hidden_elements)} hidden elements that might be blocking UI.")
    if real_errors:
        issues.append(f"{len(real_errors)} REAL console errors (JS exceptions/resource failures).")
    if suspicious_errors:
        issues.append(f"{len(suspicious_errors)} SUSPICIOUS console errors — verify with DOM before trusting.")
    if failed_requests:
        issues.append(f"Detected {len(failed_requests)} failed network requests.")

    result = {
        "detected_scenario": scenario,
        "confidence": confidence,
        "platform": "web",
        "scripts": scripts,
        "hidden_elements": hidden_elements,
        "console_errors": console_errors,
        "console_error_classification": {
            "real": [e["message"][:100] for e in real_errors[:5]],
            "suspicious": [{"msg": e["message"][:100], "reason": e["reason"]} for e in suspicious_errors[:5]],
            "total_real": len(real_errors),
            "total_suspicious": len(suspicious_errors),
        },
        "failed_requests": failed_requests,
        "potential_issues": issues,
        "summary": f"Scenario: {scenario} (conf: {confidence}%). Real errors: {len(real_errors)}, Suspicious: {len(suspicious_errors)}. Issues: {len(issues)}",
    }
    if shopify_analysis:
        result["shopify_analysis"] = shopify_analysis
    return result
