"""Settings loader — reads .env, exposes LLM config."""

import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "cohere/north-mini-code:free")
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
