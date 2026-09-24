"""OpenRouter Agent SDK client.

Uses `openrouter-agent-sdk` — runs the full tool-calling loop via call_model().
NOT the raw openai SDK. See PROJECT.md Appendix A for reference.
"""

from openrouter_agent import OpenRouter
from core.config import OPENROUTER_API_KEY

_client: OpenRouter | None = None


def get_client() -> OpenRouter:
    """Get or create the singleton OpenRouter client."""
    global _client
    if _client is None:
        _client = OpenRouter(api_key=OPENROUTER_API_KEY)
    return _client
