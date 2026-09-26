"""Reusable fake OpenRouter LLM for deterministic tests (P0-T5).

The project's actual LLM injection point is the ``call_model(client, params)``
function imported into each LLM agent module (agents.extraction,
agents.risk_flagger, agents.explainer, agents.verifier). Nothing new is
invented here — tests install a FakeLLM over that exact interface:

    fake = FakeLLM(responses=[...])
    with use_fake_llm(fake, ["agents.extraction"]):
        ...

or with unittest.mock directly:

    patch("agents.extraction.call_model", fake)

Features:
- predetermined, sequenced responses (str as-is; dict/list JSON-encoded)
- call recording: model / input / tools / stop_when / client per call
- call_count, last_prompt, prompts, models accessors
- simulate exceptions (single call via responses=[Exc()], all calls via
  exception=Exc())
- controlled failure when more calls occur than responses were programmed
"""

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

# All modules in the project that bind `call_model` / `get_client`.
AGENT_LLM_MODULES = (
    "agents.extraction",
    "agents.risk_flagger",
    "agents.explainer",
    "agents.verifier",
)


class FakeModelResult:
    """Mimics openrouter_agent's ModelResult: async get_text() -> str."""

    def __init__(self, text: str):
        self._text = text

    async def get_text(self) -> str:
        return self._text


class ForbiddenRealLLM:
    """Fails immediately if an unexpected LLM call/client creation happens.

    Installed by tests/conftest.py over every agent module during the
    deterministic suite so an accidental path to the real OpenRouter client
    can never slip through.
    """

    def __init__(self, message="Unexpected real OpenRouter call during deterministic tests"):
        self.message = message
        self.attempts = 0

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError(self.message)


class FakeLLM:
    """Deterministic, recording stand-in for ``call_model(client, params)``.

    responses: ordered list of str | dict | list | Exception.
        dict/list items are JSON-encoded (the agents expect JSON text).
        Exception items are raised when their turn comes.
    exception: if set, raised on EVERY call (used for outage simulations).
    """

    def __init__(self, responses=None, exception=None):
        self._responses = list(responses or [])
        self._exception = exception
        self.calls = []  # one dict per call: model, input, tools, stop_when, client, args, kwargs
        self.call_count = 0

    # --- call_model(client, params) interface ---

    def __call__(self, client, params, *args, **kwargs):
        self.call_count += 1
        self.calls.append({
            "model": params.get("model"),
            "input": params.get("input"),
            "tools": params.get("tools"),
            "stop_when": params.get("stop_when"),
            "client": client,
            "args": args,
            "kwargs": kwargs,
        })
        if self._exception is not None:
            raise self._exception
        if not self._responses:
            raise AssertionError(
                f"FakeLLM: unexpected extra call #{self.call_count} — "
                "no programmed response left (deterministic tests must not "
                "make more LLM calls than expected)"
            )
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, (dict, list)):
            item = json.dumps(item)
        if not isinstance(item, str):
            raise AssertionError(
                f"FakeLLM: programmed response #{self.call_count} is {item!r}, "
                "expected str/dict/list/Exception"
            )
        return FakeModelResult(item)

    # --- recording accessors ---

    @property
    def last_prompt(self):
        """The full input sent on the most recent call (or None)."""
        return self.calls[-1]["input"] if self.calls else None

    @property
    def prompts(self):
        return [c["input"] for c in self.calls]

    @property
    def models(self):
        return [c["model"] for c in self.calls]


@contextmanager
def use_fake_llm(fake, modules=None):
    """Install `fake` as call_model (plus a harmless client stub) in agent modules.

    Yields the fake for convenience. Nested unittest.mock.patch calls in
    individual tests override this installation and restore it on exit.
    """
    modules = modules if modules is not None else AGENT_LLM_MODULES
    with ExitStack() as stack:
        for module in modules:
            stack.enter_context(patch(f"{module}.call_model", fake))
            stack.enter_context(patch(f"{module}.get_client", lambda *a, **k: object()))
        yield fake
