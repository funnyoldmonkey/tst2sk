"""TST2SK Setup — Switch provider, model, and settings. API keys are managed directly in .env."""
import os
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()

# ─── Provider definitions ───────────────────────────────────────────────────

PROVIDERS = {
    "1": {
        "name": "Google_API",
        "key_var": "AI_API_KEY_GOOGLE",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            "gemini-3.1-flash-lite",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        ],
        "supports_round_robin": True,
    },
    "2": {
        "name": "OpenRouter",
        "key_var": "AI_API_KEY_OPENROUTER",
        "base_url": "https://openrouter.ai/api/v1/",
        "models": [
            "openrouter/free",
            "poolside/laguna-m.1:free",
            "openrouter/owl-alpha",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "minimax/minimax-m2.5:free",
            "inclusionai/ring-2.6-1t:free",
        ],
        "supports_round_robin": False,
    },
    "3": {
        "name": "Local_(LMStudio)",
        "key_var": "AI_API_KEY_LMSTUDIO",
        "base_url": "http://localhost:1234/v1/",
        "models": [],
        "supports_round_robin": False,
    },
}

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


# ─── .env read/write ────────────────────────────────────────────────────────

def read_env() -> dict:
    """Read .env file into a dict, preserving all keys."""
    env = {}
    if not os.path.exists(ENV_FILE):
        return env
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def write_env(env: dict):
    """Write dict back to .env file with sections."""
    lines = [
        "# TST2SK Configuration",
        "# API keys — edit these directly. Do NOT share this file.",
        "#",
        f"# Active provider: {env.get('AI_LLM_PROVIDER', 'Unknown')}",
        "",
        "# ─── API Keys (edit directly, comma-separated for multiple) ─────",
        f"AI_API_KEY_GOOGLE={env.get('AI_API_KEY_GOOGLE', '')}",
        f"AI_API_KEY_OPENROUTER={env.get('AI_API_KEY_OPENROUTER', '')}",
        f"AI_API_KEY_LMSTUDIO={env.get('AI_API_KEY_LMSTUDIO', 'lm-studio')}",
        "",
        "# ─── Active Provider Settings ───────────────────────────────────",
        f"AI_LLM_PROVIDER={env.get('AI_LLM_PROVIDER', '')}",
        f"AI_BASE_URL={env.get('AI_BASE_URL', '')}",
        f"AI_MODEL={env.get('AI_MODEL', '')}",
        f"AI_MULTIMODAL_MODEL={env.get('AI_MULTIMODAL_MODEL', 'true')}",
        "",
        "# ─── Google Round-Robin ─────────────────────────────────────────",
        f"AI_ROUND_ROBIN_SWITCH={env.get('AI_ROUND_ROBIN_SWITCH', '3')}",
        "",
        "# ─── Optional ───────────────────────────────────────────────────",
        f"AI_EXTRA_HEADERS={env.get('AI_EXTRA_HEADERS', '')}",
        "",
    ]
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─── Connection test ─────────────────────────────────────────────────────────

async def test_connection(api_key: str, base_url: str, model: str, label: str = "") -> bool:
    """Send a tiny test request to verify key + model work."""
    from openai import AsyncOpenAI
    prefix = f"  🔑 {label}: " if label else "  🔑 "
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Say OK"}],
                max_tokens=5,
            ),
            timeout=15,
        )
        text = response.choices[0].message.content or ""
        print(f"{prefix}✅ OK — got response: \"{text.strip()[:30]}\"")
        return True
    except asyncio.TimeoutError:
        print(f"{prefix}❌ Timeout (15s)")
        return False
    except Exception as e:
        err = str(e).split(" - ")[0][:80] if " - " in str(e) else str(e)[:80]
        print(f"{prefix}❌ {err}")
        return False


# ─── Main setup flow ─────────────────────────────────────────────────────────

def get_keys_for_provider(env: dict, provider: dict) -> list:
    """Get API keys for a provider from env."""
    raw = env.get(provider["key_var"], "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def main():
    env = read_env()

    # Current state
    current_provider = env.get("AI_LLM_PROVIDER", "")
    current_model = env.get("AI_MODEL", "")
    current_multimodal = env.get("AI_MULTIMODAL_MODEL", "true")
    current_rr = env.get("AI_ROUND_ROBIN_SWITCH", "3")

    print()
    print("═══════════════════════════════════════")
    print("         TST2SK Setup")
    print("═══════════════════════════════════════")

    if current_provider:
        mode_str = "Multimodal" if current_multimodal.lower() in ("true", "1", "yes") else "Text-Only"
        print(f"\n  Current: {current_provider} / {current_model} / {mode_str}")

    # ─── Step 1: Pick provider ───
    print("\n  Providers:")
    for num, prov in PROVIDERS.items():
        keys = get_keys_for_provider(env, prov)
        key_info = f"{len(keys)} key{'s' if len(keys) != 1 else ''}" if keys else "no key"
        marker = " ◀" if prov["name"] == current_provider else ""
        print(f"    [{num}] {prov['name']:20s} ({key_info}){marker}")

    # Find current provider number for default
    current_num = ""
    for num, prov in PROVIDERS.items():
        if prov["name"] == current_provider:
            current_num = num
            break

    prompt = f"\n  Select provider [{current_num}]: " if current_num else "\n  Select provider: "
    choice = input(prompt).strip()
    if not choice and current_num:
        choice = current_num
    if choice not in PROVIDERS:
        print("  ❌ Invalid choice.")
        return

    provider = PROVIDERS[choice]
    keys = get_keys_for_provider(env, provider)

    if not keys:
        print(f"\n  ⚠️  No API keys found for {provider['name']}.")
        print(f"  Add your key(s) to .env under {provider['key_var']}")
        print(f"  Then re-run this setup.")
        return

    # ─── Step 2: Pick model ───
    default_model = current_model if provider["name"] == current_provider else ""

    if provider["models"]:
        print(f"\n  Suggested models for {provider['name']}:")
        for i, m in enumerate(provider["models"], 1):
            print(f"    {i}. {m}")

    model_prompt = f"\n  Model [{default_model}]: " if default_model else "\n  Model: "
    model_input = input(model_prompt).strip()

    if not model_input and default_model:
        model = default_model
    elif model_input.isdigit() and provider["models"]:
        idx = int(model_input) - 1
        if 0 <= idx < len(provider["models"]):
            model = provider["models"][idx]
        else:
            model = model_input
    elif model_input:
        model = model_input
    else:
        print("  ❌ Model is required.")
        return

    # ─── Step 3: Google round-robin ───
    rr_switch = current_rr
    if provider["supports_round_robin"] and len(keys) > 1:
        rr_prompt = f"\n  Round-robin switch every N requests [{current_rr}]: "
        rr_input = input(rr_prompt).strip()
        if rr_input:
            try:
                rr_switch = str(max(1, int(rr_input)))
            except ValueError:
                print("  ⚠️  Invalid number, keeping current value.")

    # ─── Step 4: Multimodal ───
    mm_prompt = f"\n  Multimodal - vision mode [{current_multimodal}]: "
    mm_input = input(mm_prompt).strip().lower()
    if mm_input in ("true", "false", "1", "0", "yes", "no"):
        multimodal = mm_input
    elif not mm_input:
        multimodal = current_multimodal
    else:
        print("  ⚠️  Invalid value, keeping current.")
        multimodal = current_multimodal

    # ─── Step 5: Test connections ───
    print(f"\n  Testing {provider['name']} with model '{model}'...")

    all_passed = True
    if provider["supports_round_robin"] and len(keys) > 1:
        # Test each key
        for i, key in enumerate(keys, 1):
            passed = asyncio.run(test_connection(key, provider["base_url"], model, f"Key {i}/{len(keys)}"))
            if not passed:
                all_passed = False
    else:
        passed = asyncio.run(test_connection(keys[0], provider["base_url"], model))
        if not passed:
            all_passed = False

    if not all_passed:
        proceed = input("\n  ⚠️  Some tests failed. Save anyway? [y/N]: ").strip().lower()
        if proceed not in ("y", "yes"):
            print("  Setup cancelled.")
            return

    # ─── Step 6: Save ───
    env["AI_LLM_PROVIDER"] = provider["name"]
    env["AI_BASE_URL"] = provider["base_url"]
    env["AI_MODEL"] = model
    env["AI_MULTIMODAL_MODEL"] = multimodal
    env["AI_ROUND_ROBIN_SWITCH"] = rr_switch

    write_env(env)

    print(f"\n  ✅ Saved to .env")
    print(f"     Provider : {provider['name']}")
    print(f"     Model    : {model}")
    mode_str = "Multimodal" if multimodal in ("true", "1", "yes") else "Text-Only"
    print(f"     Mode     : {mode_str}")
    if provider["supports_round_robin"] and len(keys) > 1:
        print(f"     Keys     : {len(keys)} (rotate every {rr_switch} requests)")
    print()


if __name__ == "__main__":
    main()
