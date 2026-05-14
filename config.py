"""Configuration for TST2SK."""
import os
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

@dataclass
class AppConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    extra_headers: dict = field(default_factory=dict)
    multimodal: bool = True  # True = send screenshots (vision model), False = text-only
    headless: bool = False  # Show browser window by default
    max_turns: int = 0  # Unused — agent runs until naturally done (kept for future use)
    max_history: int = 20  # Max conversation turns to keep in context

    @classmethod
    def from_env(cls) -> "AppConfig":
        headers = {}
        raw_headers = os.getenv("AI_EXTRA_HEADERS", "")
        if raw_headers:
            try:
                headers = json.loads(raw_headers)
            except json.JSONDecodeError:
                pass
        multimodal_raw = os.getenv("AI_MULTIMODAL_MODEL", "true").lower()
        return cls(
            api_key=os.getenv("AI_API_KEY", ""),
            base_url=os.getenv("AI_BASE_URL", ""),
            model=os.getenv("AI_MODEL", ""),
            extra_headers=headers,
            multimodal=multimodal_raw in ("true", "1", "yes"),
        )
