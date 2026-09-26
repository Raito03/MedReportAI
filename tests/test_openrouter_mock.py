"""P0-T5 — Deterministic OpenRouter/LLM infrastructure tests (Mode A).

Never calls OpenRouter, never requires an API key, never requires internet.
Uses the reusable FakeLLM from tests/fake_llm.py over the project's actual
`call_model(client, params)` injection point. Complements — does not
duplicate — the P0-T4 seam tests.
"""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as app_config
from core.schemas import ExtractedLabValue, FinalExplanation, RiskFlaggedValue, VerifierResult
from agents.extraction import extract_lab_values
from agents.extraction import _extract_json as extraction_extract_json
from agents.verifier import _extract_json as verifier_extract_json
from agents.explainer import explain
from agents.reference_range import lookup_ranges
from agents.risk_flagger import classify_risk
from agents.verifier import verify
from tests.fake_llm import FakeLLM, ForbiddenRealLLM, use_fake_llm

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_KEY = "sk-test-DO-NOT-LEAK"

GLUCOSE_ITEM = {"test_name": "Glucose", "loinc_code": "2345-7",
                "value": 92.0, "unit": "mg/dL"}
RISK_ITEM = {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0,
             "unit": "mg/dL", "status": "normal",
             "reasoning": "92 is between 70 and 100"}
EXPLANATION_ITEM = {
    "test_name": "Glucose",
    "explanation": "Glucose measures blood sugar levels; results in this range are generally considered normal.",
    "doctor_questions": ["What is my target range?"],
    "citation": "Lab Reference (https://example.org/lab)",
}
VERIFIER_PASS_TEXT = '{"passed": true, "issues_found": []}'


def _run_extraction(raw_text="synthetic report text", responses=None, fake=None):
    """Run the extraction agent with a FakeLLM installed; returns (values, fake)."""
    fake = fake or FakeLLM(responses=responses)
    with use_fake_llm(fake, ["agents.extraction"]):
        values = asyncio.run(extract_lab_values(raw_text))
    return values, fake


# ============================================================
# Fake LLM basics (Step 3)
# ============================================================

def test_fake_llm_returns_predetermined_response():
    fake = FakeLLM(responses=["hello from the fake"])
    result = fake(object(), {"model": "m", "input": "prompt"})
    text = asyncio.run(result.get_text())
    assert text == "hello from the fake"
    assert fake.call_count == 1
    print("  PASS: fake LLM returns the predetermined response")


def test_fake_llm_records_calls_and_prompts():
    """Recording captures the actual params the repository sends."""
    fake = FakeLLM(responses=[GLUCOSE_ITEM])
    with use_fake_llm(fake, ["agents.extraction"]):
        asyncio.run(extract_lab_values("Glucose report text"))

    assert fake.call_count == 1
    assert "Glucose" in fake.last_prompt
    call = fake.calls[0]
    assert call["model"] == app_config.OPENROUTER_MODEL
    assert isinstance(call["input"], str) and call["input"]
    assert isinstance(call["tools"], list) and call["tools"]
    assert call["stop_when"] is not None
    assert fake.models == [app_config.OPENROUTER_MODEL]
    print("  PASS: fake LLM records model/input/tools/stop_when per call")


def test_fake_llm_sequences_multiple_responses():
    """Responses are consumed in order — A, then B, then C."""
    fake = FakeLLM(responses=["A", "B", "C"])
    got = [asyncio.run(fake(object(), {"input": f"p{i}"}).get_text())
           for i in range(3)]
    assert got == ["A", "B", "C"]
    assert fake.call_count == 3
    assert [c["input"] for c in fake.calls] == ["p0", "p1", "p2"]
    print("  PASS: fake LLM sequences multiple responses deterministically")


def test_fake_llm_unexpected_extra_call_fails_controlled():
    """More calls than programmed responses -> controlled test failure."""
    fake = FakeLLM(responses=["only one"])
    asyncio.run(fake(object(), {"input": "first"}).get_text())
    with pytest.raises(AssertionError, match="unexpected extra call"):
        fake(object(), {"input": "second"})
    print("  PASS: unexpected extra call fails in a controlled way")


def test_fake_llm_simulated_exception():
    """An Exception programmed in responses is raised on its turn."""
    fake = FakeLLM(responses=["ok", TimeoutError("simulated timeout")])
    assert asyncio.run(fake(object(), {"input": "1"}).get_text()) == "ok"
    with pytest.raises(TimeoutError, match="simulated timeout"):
        fake(object(), {"input": "2"})
    assert fake.call_count == 2
    print("  PASS: fake LLM can simulate exceptions per call")


# ============================================================
# Response parsing (Step 7) — mocked, never calls OpenRouter
# ============================================================

def test_extract_json_valid_response():
    payload = json.dumps([GLUCOSE_ITEM])
    assert extraction_extract_json(payload) == [GLUCOSE_ITEM]
    assert verifier_extract_json(VERIFIER_PASS_TEXT) == \
        {"passed": True, "issues_found": []}
    # Markdown-fenced JSON is also accepted:
    assert extraction_extract_json("```json\n" + payload + "\n```") == [GLUCOSE_ITEM]
    print("  PASS: valid response parses successfully")


def test_extract_json_empty_response_controlled_failure():
    with pytest.raises(ValueError):
        extraction_extract_json("")
    with pytest.raises(ValueError):
        verifier_extract_json("")
    print("  PASS: empty response -> controlled failure")


def test_extract_json_malformed_response_controlled_failure():
    with pytest.raises(ValueError):
        extraction_extract_json("not json at all, just prose")
    with pytest.raises(ValueError):
        verifier_extract_json("{{{{ broken")
    print("  PASS: malformed response -> controlled failure")


def test_extract_json_missing_expected_content_controlled_failure():
    # Fenced block whose contents are not JSON:
    with pytest.raises(ValueError):
        extraction_extract_json("```json\nsorry, no result\n```")
    print("  PASS: missing expected content -> controlled failure")


# ============================================================
# API failure modes (Step 8) — via the fake client, no network
# ============================================================

def _sdk_http_error(exc_cls, status, message):
    """Build a realistic openrouter.errors exception (SDK's actual types)."""
    import httpx
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, request=request)
    from types import SimpleNamespace
    data = SimpleNamespace(error=SimpleNamespace(message=message))
    return exc_cls(data, response)


def _assert_failure_controlled(exception):
    """Run extraction with the fake raising `exception`; assert controlled surfacing."""
    fake = FakeLLM(exception=exception)
    with use_fake_llm(fake, ["agents.extraction"]):
        with pytest.raises(type(exception)) as exc_info:
            asyncio.run(extract_lab_values("synthetic report text"))
    assert fake.call_count == 1, "API failure must surface on the first attempt (no invented retry)"
    return exc_info


def test_timeout_failure_controlled():
    _assert_failure_controlled(TimeoutError("Request timed out"))
    print("  PASS: timeout surfaces as a controlled error (single attempt)")


def test_connection_failure_controlled():
    _assert_failure_controlled(ConnectionError("Connection refused"))
    print("  PASS: connection failure surfaces as a controlled error (single attempt)")


@pytest.mark.parametrize("status,exc_name,message", [
    (401, "UnauthorizedResponseError", "invalid api key"),
    (403, "ForbiddenResponseError", "forbidden"),
    (429, "TooManyRequestsResponseError", "rate limited"),
    (500, "InternalServerResponseError", "internal server error"),
    (503, "ServiceUnavailableResponseError", "service unavailable"),
    (408, "RequestTimeoutResponseError", "request timeout"),
])
def test_http_error_failures_controlled(status, exc_name, message):
    """HTTP 401/403/429/5xx/408 use the SDK's real exception types and surface once."""
    import openrouter.errors as openrouter_errors
    exc_cls = getattr(openrouter_errors, exc_name)
    exc = _sdk_http_error(exc_cls, status, f"simulated: {message}")
    exc_info = _assert_failure_controlled(exc)
    assert message in str(exc_info.value)
    # Authentication failures must be recognizable as configuration problems:
    if status in (401, 403):
        assert "api key" in str(exc_info.value) or "forbidden" in str(exc_info.value)
    print(f"  PASS: HTTP {status} ({exc_name}) surfaces as a controlled error")


# ============================================================
# API key safety (Step 9) — synthetic key only, never a real one
# ============================================================

def test_api_key_never_appears_in_prompts_or_records():
    """Even with a key configured in the environment, it never reaches the LLM path."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": SYNTHETIC_KEY}):
        values, fake = _run_extraction(responses=[[GLUCOSE_ITEM]])
    assert len(values) == 1
    dump = json.dumps(fake.calls, default=str)
    assert SYNTHETIC_KEY not in dump
    assert SYNTHETIC_KEY not in fake.last_prompt
    print("  PASS: API key never appears in prompts or recorded calls")


def test_api_key_never_appears_in_error_messages():
    """Exceptions surfaced from the LLM path do not contain the API key."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": SYNTHETIC_KEY}):
        fake = FakeLLM(exception=RuntimeError("simulated upstream failure"))
        with use_fake_llm(fake, ["agents.extraction"]):
            with pytest.raises(RuntimeError) as exc_info:
                asyncio.run(extract_lab_values("synthetic report text"))
        guard_message = ForbiddenRealLLM().message

    assert str(exc_info.value) == "simulated upstream failure"
    assert SYNTHETIC_KEY not in str(exc_info.value)
    assert SYNTHETIC_KEY not in guard_message
    print("  PASS: API key never appears in error messages")


def test_no_hardcoded_credentials_in_source_files():
    """The configured (real) key must never be committed anywhere in source."""
    real_key = app_config.OPENROUTER_API_KEY
    py_files = [p for p in REPO_ROOT.rglob("*.py")
                if ".git" not in p.parts and "__pycache__" not in p.parts]
    if real_key:
        for path in py_files:
            source = path.read_text(encoding="utf-8", errors="ignore")
            assert real_key not in source, f"API key leaked into {path}"
    # The synthetic test key must not appear in production code:
    for subdir in ("agents", "core", "pipeline", "tools", "ui"):
        for path in (REPO_ROOT / subdir).rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="ignore")
            assert SYNTHETIC_KEY not in source, f"synthetic key leaked into {path}"
    print("  PASS: no hardcoded credentials in source files")


def test_gitignore_protects_env_files():
    """.gitignore must protect .env and .env.* while keeping .env.example tracked."""
    lines = [l.strip() for l in
             (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert ".env" in lines, ".env must be gitignored"
    assert ".env.*" in lines, ".env.* variants must be gitignored"
    assert "!.env.example" in lines, ".env.example must stay tracked (exception rule)"
    print("  PASS: .gitignore protects .env files, .env.example stays tracked")


# ============================================================
# Model configuration (Step 10)
# ============================================================

def test_model_configuration_is_valid():
    """Configured model is a non-empty 'provider/model' string with a default."""
    model = app_config.OPENROUTER_MODEL
    assert isinstance(model, str) and model
    assert "/" in model
    print(f"  PASS: model configuration valid ({model})")


def test_agents_send_configured_model():
    """The model recorded at the client is exactly core.config.OPENROUTER_MODEL."""
    _, fake = _run_extraction(responses=[GLUCOSE_ITEM])
    assert fake.calls[0]["model"] == app_config.OPENROUTER_MODEL
    print("  PASS: agents send the configured model to the client")


def test_model_env_override_reloads_config():
    """OPENROUTER_MODEL env var overrides the model (config reloads cleanly)."""
    original_env = os.environ.get("OPENROUTER_MODEL")
    original_value = app_config.OPENROUTER_MODEL
    try:
        os.environ["OPENROUTER_MODEL"] = "acme/override-model:free"
        importlib.reload(app_config)
        assert app_config.OPENROUTER_MODEL == "acme/override-model:free"
    finally:
        if original_env is None:
            os.environ.pop("OPENROUTER_MODEL", None)
        else:
            os.environ["OPENROUTER_MODEL"] = original_env
        importlib.reload(app_config)
    assert app_config.OPENROUTER_MODEL == original_value
    print("  PASS: model can be overridden via environment and restored")


def test_deterministic_tests_do_not_depend_on_production_model():
    """A test can pin the model per-agent without touching production config."""
    fake = FakeLLM(responses=[GLUCOSE_ITEM])
    with use_fake_llm(fake, ["agents.extraction"]), \
         patch("agents.extraction.OPENROUTER_MODEL", "acme/test-model:free"):
        asyncio.run(extract_lab_values("synthetic report text"))
    assert fake.calls[0]["model"] == "acme/test-model:free"
    assert app_config.OPENROUTER_MODEL != "acme/test-model:free", \
        "per-agent patch must not leak into production config"
    print("  PASS: deterministic tests can override the model independently")


# ============================================================
# Agents driven by the fake LLM (Step 11)
# ============================================================

def test_extraction_agent_with_fake_llm():
    values, fake = _run_extraction(responses=[[GLUCOSE_ITEM]])
    assert fake.call_count == 1
    assert "synthetic report text" in fake.last_prompt
    assert len(values) == 1
    assert isinstance(values[0], ExtractedLabValue)
    assert values[0].loinc_code == "2345-7" and values[0].value == 92.0
    print("  PASS: extraction agent works with the fake LLM")


def test_risk_flagger_with_fake_llm():
    checked = lookup_ranges([ExtractedLabValue(**GLUCOSE_ITEM)])
    fake = FakeLLM(responses=[[RISK_ITEM]])
    with use_fake_llm(fake, ["agents.risk_flagger"]):
        results = asyncio.run(classify_risk(checked))
    assert fake.call_count == 1
    assert "Classify these lab values" in fake.last_prompt
    assert "Glucose" in fake.last_prompt
    assert '"reference_low": 70.0' in fake.last_prompt
    assert len(results) == 1
    assert isinstance(results[0], RiskFlaggedValue)
    assert results[0].status == "normal"
    print("  PASS: risk flagger works with the fake LLM")


def test_explainer_with_fake_llm():
    risk = [RiskFlaggedValue(**RISK_ITEM)]
    fake = FakeLLM(responses=[[EXPLANATION_ITEM]])
    with use_fake_llm(fake, ["agents.explainer"]), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: "Patched citation")):
        explanations = asyncio.run(explain(risk))
    assert fake.call_count == 1
    assert "Explain these lab results" in fake.last_prompt
    assert '"status": "normal"' in fake.last_prompt
    assert len(explanations) == 1
    assert isinstance(explanations[0], FinalExplanation)
    assert explanations[0].test_name == "Glucose"
    print("  PASS: explainer works with the fake LLM")


def test_verifier_with_fake_llm():
    explanations = [FinalExplanation(**EXPLANATION_ITEM)]
    risk = [RiskFlaggedValue(**RISK_ITEM)]
    fake = FakeLLM(responses=[VERIFIER_PASS_TEXT])
    with use_fake_llm(fake, ["agents.verifier"]):
        result = asyncio.run(verify(explanations, risk))
    assert fake.call_count == 1
    assert "Risk classifications" in fake.last_prompt
    assert isinstance(result, VerifierResult)
    assert result.passed is True and result.action == "approve"
    print("  PASS: verifier works with the fake LLM")


# ============================================================
# Deterministic response sequencing across stages (Step 13)
# ============================================================

def test_sequenced_responses_across_agent_stages():
    """One shared fake serves extract -> classify -> explain -> verify in order."""
    fake = FakeLLM(responses=[
        [GLUCOSE_ITEM],          # stage 1: extraction
        [RISK_ITEM],             # stage 2: risk flagger
        [EXPLANATION_ITEM],      # stage 3: explainer
        VERIFIER_PASS_TEXT,      # stage 4: verifier
    ])
    with use_fake_llm(fake), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: "Patched citation")):
        values = asyncio.run(extract_lab_values("synthetic P0-T5 report"))
        checked = lookup_ranges(values)          # no LLM
        risk = asyncio.run(classify_risk(checked))
        explanations = asyncio.run(explain(risk))
        verdict = asyncio.run(verify(explanations, risk))

    assert fake.call_count == 4
    # Each call received the response intended for its own stage:
    assert isinstance(values[0], ExtractedLabValue)
    assert risk[0].status == "normal"
    assert isinstance(explanations[0], FinalExplanation)
    assert verdict.passed is True
    # Each call carried its own stage's prompt:
    assert "synthetic P0-T5 report" in fake.calls[0]["input"]
    assert "Classify these lab values" in fake.calls[1]["input"]
    assert "Explain these lab results" in fake.calls[2]["input"]
    assert "Risk classifications" in fake.calls[3]["input"]
    print("  PASS: responses sequenced correctly across all four agent stages")


def test_more_calls_than_programmed_fails_controlled():
    """If an agent makes more LLM calls than programmed, the fake fails the test."""
    fake = FakeLLM(responses=[GLUCOSE_ITEM])  # only enough for extraction
    with use_fake_llm(fake, ["agents.extraction", "agents.risk_flagger"]):
        asyncio.run(extract_lab_values("synthetic report text"))  # consumes response
        checked = lookup_ranges([ExtractedLabValue(**GLUCOSE_ITEM)])
        with pytest.raises(AssertionError, match="unexpected extra call"):
            asyncio.run(classify_risk(checked))
    print("  PASS: more calls than programmed -> controlled test failure")


# ============================================================
# Malformed LLM output through the fake (Step 14)
# ============================================================

def test_fake_invalid_json_fails_controlled():
    fake = FakeLLM(responses=["totally not json, just prose"])
    with use_fake_llm(fake, ["agents.extraction"]):
        with pytest.raises(ValueError):
            asyncio.run(extract_lab_values("synthetic report text"))
    assert fake.call_count == 1
    print("  PASS: invalid JSON from LLM -> controlled failure")


def test_fake_missing_required_fields_fail_validation():
    fake = FakeLLM(responses=[[{"test_name": "Glucose", "loinc_code": "2345-7",
                                "value": 92.0}]])  # missing unit
    with use_fake_llm(fake, ["agents.extraction"]):
        with pytest.raises(ValidationError):
            asyncio.run(extract_lab_values("synthetic report text"))
    print("  PASS: missing schema fields -> controlled validation failure")


def test_fake_wrong_data_types_fail_validation():
    fake = FakeLLM(responses=[[{"test_name": "Glucose", "loinc_code": "2345-7",
                                "value": "not-a-number", "unit": "mg/dL"}]])
    with use_fake_llm(fake, ["agents.extraction"]):
        with pytest.raises(ValidationError):
            asyncio.run(extract_lab_values("synthetic report text"))
    print("  PASS: wrong data types -> controlled validation failure")


def test_fake_empty_object_yields_no_partial_data():
    """"{}" -> the extraction agent produces an empty result, never partial objects."""
    values, fake = _run_extraction(responses=["{}"])
    assert values == []
    assert fake.call_count == 1
    # Same for an object with only unexpected keys:
    values2, _ = _run_extraction(responses=['{"unexpected": "field"}'])
    assert values2 == []
    print("  PASS: empty/unexpected object responses produce no partial lab values")


def test_fake_unexpected_dict_risk_yields_no_invalid_classification():
    """Dict-wrapped risk response with unknown keys creates no invalid objects."""
    checked = lookup_ranges([ExtractedLabValue(**GLUCOSE_ITEM)])
    fake = FakeLLM(responses=['{"unexpected": "field"}'])
    with use_fake_llm(fake, ["agents.risk_flagger"]):
        results = asyncio.run(classify_risk(checked))
    assert results == [], "no RiskFlaggedValue may be fabricated from a malformed response"
    print("  PASS: malformed risk response yields no invalid classification")


def test_fake_empty_object_verifier_fails_closed():
    """"{}" verifier output fails closed (never silently approves)."""
    explanations = [FinalExplanation(**EXPLANATION_ITEM)]
    risk = [RiskFlaggedValue(**RISK_ITEM)]
    fake = FakeLLM(responses=["{}"])
    with use_fake_llm(fake, ["agents.verifier"]):
        result = asyncio.run(verify(explanations, risk))
    assert result.passed is False
    assert result.action == "send_back_for_correction"
    print("  PASS: empty object verifier output fails closed")


def test_fake_unexpected_field_verifier_fails_closed():
    """Verifier output without the contracted fields fails closed."""
    explanations = [FinalExplanation(**EXPLANATION_ITEM)]
    risk = [RiskFlaggedValue(**RISK_ITEM)]
    fake = FakeLLM(responses=['{"unexpected": "field"}'])
    with use_fake_llm(fake, ["agents.verifier"]):
        result = asyncio.run(verify(explanations, risk))
    assert result.passed is False
    assert result.issues_found
    print("  PASS: unexpected-field verifier output fails closed")


if __name__ == "__main__":
    print("=== P0-T5 Deterministic OpenRouter Infrastructure Tests ===\n")
    test_fake_llm_returns_predetermined_response()
    test_fake_llm_records_calls_and_prompts()
    test_fake_llm_sequences_multiple_responses()
    test_fake_llm_unexpected_extra_call_fails_controlled()
    test_fake_llm_simulated_exception()
    test_extract_json_valid_response()
    test_extract_json_empty_response_controlled_failure()
    test_extract_json_malformed_response_controlled_failure()
    test_extract_json_missing_expected_content_controlled_failure()
    test_timeout_failure_controlled()
    test_connection_failure_controlled()
    for status, name, msg in [(401, "UnauthorizedResponseError", "invalid api key"),
                              (403, "ForbiddenResponseError", "forbidden"),
                              (429, "TooManyRequestsResponseError", "rate limited"),
                              (500, "InternalServerResponseError", "internal server error"),
                              (503, "ServiceUnavailableResponseError", "service unavailable"),
                              (408, "RequestTimeoutResponseError", "request timeout")]:
        test_http_error_failures_controlled(status, name, msg)
    test_api_key_never_appears_in_prompts_or_records()
    test_api_key_never_appears_in_error_messages()
    test_no_hardcoded_credentials_in_source_files()
    test_gitignore_protects_env_files()
    test_model_configuration_is_valid()
    test_agents_send_configured_model()
    test_model_env_override_reloads_config()
    test_deterministic_tests_do_not_depend_on_production_model()
    test_extraction_agent_with_fake_llm()
    test_risk_flagger_with_fake_llm()
    test_explainer_with_fake_llm()
    test_verifier_with_fake_llm()
    test_sequenced_responses_across_agent_stages()
    test_more_calls_than_programmed_fails_controlled()
    test_fake_invalid_json_fails_controlled()
    test_fake_missing_required_fields_fail_validation()
    test_fake_wrong_data_types_fail_validation()
    test_fake_empty_object_yields_no_partial_data()
    test_fake_unexpected_dict_risk_yields_no_invalid_classification()
    test_fake_empty_object_verifier_fails_closed()
    test_fake_unexpected_field_verifier_fails_closed()
    print("\nAll P0-T5 deterministic OpenRouter tests passed.")
