"""OpenRouter smoke test — Mode B (explicit integration, real API).

NEVER runs during a normal pytest session. Enable explicitly:

    OPENROUTER_RUN_INTEGRATION=1 pytest -q tests/integration/test_openrouter_smoke.py

Skips clearly when OPENROUTER_API_KEY is not configured. Uses a harmless
synthetic prompt (no patient data, no medical decisions) and validates only
connectivity/basic response parsing. The API key is never printed.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.config import OPENROUTER_API_KEY, OPENROUTER_MODEL  # noqa: E402

RUN_INTEGRATION = os.getenv("OPENROUTER_RUN_INTEGRATION", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="OpenRouter integration test — set OPENROUTER_RUN_INTEGRATION=1 to run "
           "(requires OPENROUTER_API_KEY)",
)


def test_openrouter_smoke():
    """Minimal connectivity check: client -> configured model -> valid response."""
    if not OPENROUTER_API_KEY:
        pytest.skip("SKIPPED: OPENROUTER_API_KEY is not configured")

    from core.llm_client import get_client
    from openrouter_agent import call_model, step_count_is

    result = call_model(
        get_client(),
        {
            "model": OPENROUTER_MODEL,
            "input": 'Return exactly the JSON: {"status":"ok"}',
            "stop_when": step_count_is(1),
        },
    )
    text = asyncio.run(result.get_text())

    assert text, "OpenRouter returned an empty response"
    assert "ok" in text.lower(), f"unexpected response: {text[:200]!r}"
    # The API key must never surface through the model:
    assert OPENROUTER_API_KEY not in text
    # Never log the key here.
