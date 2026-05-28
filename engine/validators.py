"""Shared validation utilities for action pre-flight checks.

Centralizes checks that need to run in multiple places (brain.py pre-flight,
browser/actions.py execution, engine/jit.py rules) so the logic lives once.
"""

import re


def check_body_resize_css(css: str) -> str | None:
    """Check if CSS modifies body/html width. Returns error message or None."""
    normalized = re.sub(r'\s+', ' ', css).lower()
    if re.search(r'\b(body|html)\b\s*\{[^}]*\b(width|min-width|max-width)\b', normalized):
        return (
            "[error] Action rejected: Modifying the width (width, min-width, max-width) "
            "of the <body> or <html> element via CSS is strictly prohibited. Changing body dimensions "
            "leads to broken layouts and layout collapse. Use proper browser tools if you need to "
            "adjust viewport size."
        )
    return None


def check_body_resize_js(code: str) -> str | None:
    """Check if JS modifies body/html width. Returns error message or None."""
    normalized = re.sub(r'\s+', ' ', code).lower()
    if (
        "body.style.width" in normalized
        or "body.style.minwidth" in normalized
        or "body.style.maxwidth" in normalized
        or "html.style.width" in normalized
        or "html.style.minwidth" in normalized
        or "html.style.maxwidth" in normalized
        or "body.style =" in normalized
        or "html.style =" in normalized
        or (re.search(r'\b(body|html)\.style\b', normalized) and re.search(r'\b(width|minwidth|maxwidth)\b', normalized))
        or re.search(r'queryselector\(\s*[\'"](body|html)[\'"]\s*\)\.style', normalized)
        or re.search(r'style\.setproperty\(\s*[\'"](min-|max-)?width[\'"]', normalized)
        or re.search(r'setattribute\(\s*[\'"]style[\'"]\s*,\s*[\'"][^\'"]*\b(width|min-width|max-width)\b', normalized)
    ):
        return (
            "[error] Action rejected: Modifying the width of the <body> or <html> elements "
            "via JavaScript style properties is strictly prohibited. Modifying body dimensions "
            "bypassing Playwright viewport commands causes layout collapse. Please use standard viewport "
            "settings or adjust elements themselves instead of resizing the root body/html layout."
        )
    return None
