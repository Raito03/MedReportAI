"""P0-T5 test infrastructure: deterministic tests can never reach OpenRouter.

An autouse fixture replaces `call_model` and `get_client` in every LLM agent
module with a guard that fails immediately. Individual tests override the
guard with their own FakeLLM/mock via unittest.mock.patch (nested patches),
so existing P0-T2/P0-T4 tests are unaffected.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.fake_llm import AGENT_LLM_MODULES, ForbiddenRealLLM  # noqa: E402


@pytest.fixture(autouse=True)
def forbid_real_openrouter():
    """Fail immediately on any unexpected real LLM call during deterministic tests."""
    call_guard = ForbiddenRealLLM()
    client_guard = ForbiddenRealLLM(
        "Unexpected real LLM client creation during deterministic tests"
    )
    with ExitStack() as stack:
        for module in AGENT_LLM_MODULES:
            stack.enter_context(patch(f"{module}.call_model", call_guard))
            stack.enter_context(patch(f"{module}.get_client", client_guard))
        yield call_guard
