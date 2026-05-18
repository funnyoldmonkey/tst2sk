"""Configuration for TST2SK."""
import os
import sys
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

# Map provider names to their key env variables
_PROVIDER_KEY_MAP = {
    "Google_API": "AI_API_KEY_GOOGLE",
    "OpenRouter": "AI_API_KEY_OPENROUTER",
    "Local_(LMStudio)": "AI_API_KEY_LMSTUDIO",
}


@dataclass
class AppConfig:
    api_key: str = ""
    api_keys: list = field(default_factory=list)  # All keys for current provider
    base_url: str = ""
    model: str = ""
    extra_headers: dict = field(default_factory=dict)
    multimodal: bool = True  # True = send screenshots (vision model), False = text-only
    headless: bool = False  # Show browser window by default
    max_turns: int = 0  # Unused — agent runs until naturally done (kept for future use)
    max_history: int = 20  # Max conversation turns to keep in context
    round_robin_switch: int = 3  # Rotate API key after N requests (Google only)

    @classmethod
    def from_env(cls) -> "AppConfig":
        headers = {}
        raw_headers = os.getenv("AI_EXTRA_HEADERS", "")
        if raw_headers:
            try:
                headers = json.loads(raw_headers)
            except json.JSONDecodeError:
                print(f"⚠️  Warning: AI_EXTRA_HEADERS is not valid JSON — headers ignored.", file=sys.stderr)
        multimodal_raw = os.getenv("AI_MULTIMODAL_MODEL", "true").lower()

        # Resolve API key(s) based on provider
        provider = os.getenv("AI_LLM_PROVIDER", "")
        key_var = _PROVIDER_KEY_MAP.get(provider, "")
        api_keys_raw = os.getenv(key_var, "") if key_var else ""

        # Parse comma-separated keys
        api_keys = [k.strip() for k in api_keys_raw.split(",") if k.strip()]

        # Fallback to legacy AI_API_KEY if provider-specific key not found
        if not api_keys:
            legacy_key = os.getenv("AI_API_KEY", "")
            if legacy_key:
                api_keys = [legacy_key]

        # Round-robin only applies to Google
        rr_switch = 3
        if provider == "Google_API":
            try:
                rr_switch = max(1, int(os.getenv("AI_ROUND_ROBIN_SWITCH", "3")))
            except ValueError:
                rr_switch = 3

        return cls(
            api_key=api_keys[0] if api_keys else "",
            api_keys=api_keys,
            base_url=os.getenv("AI_BASE_URL", ""),
            model=os.getenv("AI_MODEL", ""),
            extra_headers=headers,
            multimodal=multimodal_raw in ("true", "1", "yes"),
            round_robin_switch=rr_switch,
        )
