"""Settings loader — reads .env, exposes LLM config."""

import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "cohere/north-mini-code:free")
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# Values that mean "no real key": unset/blank, and the placeholder shipped in
# .env.example. Treating the placeholder as unconfigured turns a fresh
# "copy .env.example -> .env" mistake into a clear startup message instead of
# a mid-pipeline auth error deep in the SDK (P1-T5 CLI verification).
_UNCONFIGURED_KEYS = {"", "your-key-here"}


def llm_configured() -> bool:
    """True when a real OpenRouter API key is available."""
    return OPENROUTER_API_KEY.strip() not in _UNCONFIGURED_KEYS
