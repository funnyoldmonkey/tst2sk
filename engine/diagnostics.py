"""Cross-reference diagnostic engine — auto-detects scenario type."""
import os
import re
from urllib.parse import urlparse

SCRATCH_DIR = "scratch"

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
    
    # --- Scenario Detection ---
    scenario = "generic_web_page"
    confidence = 0
    
    # Shopify detection
    if "shopify" in dom_content.lower() or "shopify" in network_content.lower():
        scenario = "shopify_store"
        confidence = 80
    
    # Checkout/Cart detection
    if any(k in dom_content.lower() for k in ["cart", "checkout", "payment", "shipping"]):
        scenario = "ecommerce_checkout"
        confidence = 70
        
    # Auth/Login detection
    if any(k in dom_content.lower() for k in ["login", "sign-in", "password", "auth"]):
        scenario = "auth_flow"
        confidence = 60

    # --- Potential Issues ---
    issues = []
    if hidden_elements:
        issues.append(f"Found {len(hidden_elements)} hidden elements that might be blocking UI.")
    if console_errors:
        issues.append(f"Detected {len(console_errors)} console errors.")
    if failed_requests:
        issues.append(f"Detected {len(failed_requests)} failed network requests.")

    return {
        "detected_scenario": scenario,
        "confidence": confidence,
        "platform": "web",
        "scripts": scripts,
        "hidden_elements": hidden_elements,
        "console_errors": console_errors,
        "failed_requests": failed_requests,
        "potential_issues": issues,
        "summary": f"Scenario: {scenario} (conf: {confidence}%). Issues: {len(issues)}",
    }
