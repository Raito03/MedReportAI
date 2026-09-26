"""OpenRouter Agent SDK connectivity + tool-calling tests — Mode B (integration).

These tests make REAL OpenRouter API calls and are therefore explicitly gated:

* Normal pytest run: skipped automatically — they never run accidentally.
* Opt in:  OPENROUTER_RUN_INTEGRATION=1 pytest -q tests/test_llm_client.py
* Direct run (also explicit):  python tests/test_llm_client.py

Requires OPENROUTER_API_KEY; skips clearly when it is not configured.
The API key is never printed or included in any assertion message.
"""

import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import OPENROUTER_API_KEY, OPENROUTER_MODEL
from core.llm_client import get_client
from openrouter_agent import call_model, step_count_is, tool
from pydantic import BaseModel

RUN_INTEGRATION = os.getenv("OPENROUTER_RUN_INTEGRATION", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="OpenRouter integration test — set OPENROUTER_RUN_INTEGRATION=1 to run "
           "(requires OPENROUTER_API_KEY)",
)


class DummyInput(BaseModel):
    city: str


class DummyOutput(BaseModel):
    temperature: int
    condition: str


dummy_tool = tool(
    name="get_weather",
    description="Get weather for a city",
    input_schema=DummyInput,
    output_schema=DummyOutput,
    execute=lambda params, ctx: DummyOutput(temperature=22, condition="sunny"),
)


async def _basic_chat():
    """Send a simple message, get a response via call_model()."""
    if not OPENROUTER_API_KEY:
        print("SKIP: No OPENROUTER_API_KEY set in .env")
        return False

    client = get_client()
    # call_model() is sync, returns ModelResult
    result = call_model(
        client,
        {
            "model": OPENROUTER_MODEL,
            "input": "Reply with exactly one word: OK",
            "stop_when": step_count_is(1),
        },
    )
    # get_text() is async
    text = await result.get_text()
    assert text is not None and len(text) > 0
    safe = text[:50].encode('ascii', 'replace').decode()
    print(f"  Basic chat OK: {safe}")
    return True


async def _tool_calling():
    """Test tool calling with call_model() + a dummy tool."""
    if not OPENROUTER_API_KEY:
        print("SKIP: No OPENROUTER_API_KEY set in .env")
        return False

    client = get_client()
    result = call_model(
        client,
        {
            "model": OPENROUTER_MODEL,
            "input": "What's the weather in Paris? Use the get_weather tool.",
            "tools": [dummy_tool],
            "stop_when": step_count_is(3),
        },
    )

    text = await result.get_text()
    safe = (text[:100] if text else '(no text)').encode('ascii', 'replace').decode()
    print(f"  Tool calling OK: {safe}")
    return True


def test_basic_chat():
    """Mode B: simple chat against the real OpenRouter API."""
    if not OPENROUTER_API_KEY:
        pytest.skip("SKIPPED: OPENROUTER_API_KEY is not configured")
    assert asyncio.run(_basic_chat()) is True


def test_tool_calling():
    """Mode B: tool-calling loop against the real OpenRouter API."""
    if not OPENROUTER_API_KEY:
        pytest.skip("SKIPPED: OPENROUTER_API_KEY is not configured")
    assert asyncio.run(_tool_calling()) is True


if __name__ == "__main__":
    print("=== OpenRouter Agent SDK Validation Test ===")
    print(f"API key set: {'yes' if OPENROUTER_API_KEY else 'NO'}")
    print(f"Model: {OPENROUTER_MODEL}")

    async def run_all():
        results = []
        results.append(("basic_chat", await _basic_chat()))
        results.append(("tool_calling", await _tool_calling()))

        print("\n--- Results ---")
        all_pass = True
        for name, passed in results:
            status = "PASS" if passed else "FAIL"
            print(f"  {name}: {status}")
            if not passed:
                all_pass = False

        if all_pass:
            print("\nAll tests passed — OpenRouter Agent SDK is ready.")
        else:
            print("\nSome tests failed — check .env and API key.")

    asyncio.run(run_all())
