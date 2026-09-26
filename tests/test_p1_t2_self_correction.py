"""P1-T2 — Verifier Self-Correction (deterministic — no OpenRouter, no network).

Runs the REAL production pipeline (pipeline.orchestrator.run_pipeline)
end-to-end with the P0-T5 FakeLLM and a canned MedlinePlus HTTP boundary —
the same harness style as the P0-T7 integration gate — on the project's
existing synthetic PDF (data/samples/normal_report.pdf). No real patient
data, no randomness, no external services.

Scenarios:
1. The first generated explanation is intentionally invalid (diagnostic
   language caught by the verifier's deterministic code check) ->
   verifier rejects -> its issues_found are injected into the correction
   prompt -> the correction attempt produces a valid explanation ->
   verifier accepts -> the final pipeline result contains the corrected
   explanation (verified=True).
2. The correction attempt also fails -> the bounded retry limit is reached
   -> the pipeline returns a clear controlled failure (verified=False +
   issues) instead of silently accepting the invalid explanation.
3. Retry-limit tests: the MAX_RETRIES bound is respected (0 and raised
   bounds), so the correction loop can never run unbounded.
4. Safety invariants: the correction prompt restates — never relaxes — the
   rules, structured lab/risk data passes through unchanged, and the final
   citation still comes from the trusted lookup layer, never the LLM.
"""

import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline.orchestrator as orch
from core.schemas import FinalExplanation
from pipeline.orchestrator import MAX_RETRIES, run_pipeline
from tests.fake_llm import FakeLLM, use_fake_llm

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNTHETIC_PDF = os.path.join(REPO_ROOT, "data", "samples", "normal_report.pdf")

# ---------------------------------------------------------------------------
# Controlled MedlinePlus boundary (real get_citation path, canned HTTP —
# same pattern as the P0-T7 integration gate)
# ---------------------------------------------------------------------------

_TRUSTED_PAGES = {
    "2345-7": ("P1T2 Fixture Glucose Guide", "https://medlineplus.gov/p1t2-glucose"),
}
TRUSTED_CITATION = (
    "MedlinePlus: P1T2 Fixture Glucose Guide (https://medlineplus.gov/p1t2-glucose)"
)


def _canned_urlopen(request, timeout=None, context=None):
    """Canned MedlinePlus Connect response, chosen by the requested LOINC."""
    url = getattr(request, "full_url", str(request))
    entry = None
    for loinc, (title, page_url) in _TRUSTED_PAGES.items():
        if loinc in url:
            entry = {
                "title": {"_value": title},
                "link": [{"href": page_url}],
                "summary": {"_value": "P1-T2 synthetic grounding entry."},
            }
            break
    payload = {"feed": {"entry": [entry] if entry else []}}
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = lambda self: self
    response.__exit__ = MagicMock(return_value=False)
    return response


# ---------------------------------------------------------------------------
# Deterministic LLM responses (FakeLLM consumes them in pipeline order)
# ---------------------------------------------------------------------------

EXTRACT_ITEMS = [
    {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"}
]
RISK_ITEMS = [
    {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
     "status": "normal",
     "reasoning": "92.0 is within the 70-100 mg/dL reference range"}
]
# Intentionally INVALID draft: matches the verifier's deterministic
# DIAGNOSTIC_PATTERNS check ('\byou have\b') regardless of the LLM verdict.
INVALID_EXPLANATION_ITEMS = [
    {"test_name": "Glucose",
     "explanation": "You have diabetes because your glucose level is 92 mg/dL.",
     "doctor_questions": ["Should I monitor my glucose more often?"],
     "citation": "https://evil.example/rogue-citation"}
]
# Valid correction: hedged language, no diagnostic claim.
CORRECTED_EXPLANATION_ITEMS = [
    {"test_name": "Glucose",
     "explanation": "Glucose measures blood sugar levels; results in this range "
                    "are generally considered normal.",
     "doctor_questions": ["How often should glucose be checked?"],
     "citation": "https://evil.example/rogue-citation"}
]
# The verifier's LLM says "pass" in every scenario — rejection/acceptance is
# driven by the deterministic code check, so results never depend on a
# programmed LLM verdict.
VERIFIER_PASS = json.dumps({"passed": True, "issues_found": []})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(fake):
    """Run the REAL orchestrator over the existing synthetic PDF with FakeLLM
    and a canned MedlinePlus boundary (no network, no OpenRouter)."""
    assert os.path.exists(SYNTHETIC_PDF), f"missing synthetic fixture: {SYNTHETIC_PDF}"
    with use_fake_llm(fake), \
         patch("tools.medlineplus_connect.urllib.request.urlopen",
               side_effect=_canned_urlopen):
        return asyncio.run(run_pipeline(SYNTHETIC_PDF))


def _explain_prompt_parts(prompt):
    """Split an explain() prompt into (risk_context_json, correction_section)."""
    body = prompt.split("Explain these lab results:\n", 1)[1]
    marker = "\n\nCORRECTION REQUESTED"
    if marker in body:
        context, correction = body.split(marker, 1)
        return context, marker + correction
    return body, ""


def _verifier_payloads(call):
    """Parse (explanations, risk_classifications) from a verifier prompt."""
    prompt = call["input"]
    expl = prompt.split("Explanations:\n", 1)[1].split("\n\nRisk classifications:\n")[0]
    risk = prompt.split("Risk classifications:\n", 1)[1]
    return json.loads(expl), json.loads(risk)


def _count_calls(fake, marker):
    """How many recorded LLM calls carried `marker` in their prompt."""
    return sum(1 for c in fake.calls if marker in (c["input"] or ""))


def _success_scenario_fake():
    """One invalid draft, one accepted correction (6 LLM calls total)."""
    return FakeLLM(responses=[
        EXTRACT_ITEMS, RISK_ITEMS,
        INVALID_EXPLANATION_ITEMS, VERIFIER_PASS,    # rejected by code check
        CORRECTED_EXPLANATION_ITEMS, VERIFIER_PASS,  # correction accepted
    ])


def _always_failing_fake(attempts):
    """`attempts` explain+verify cycles, each rejected by the code check."""
    responses = [EXTRACT_ITEMS, RISK_ITEMS]
    for _ in range(attempts):
        responses += [INVALID_EXPLANATION_ITEMS, VERIFIER_PASS]
    return FakeLLM(responses=responses)


# ---------------------------------------------------------------------------
# 1. Successful self-correction (the demo moment)
# ---------------------------------------------------------------------------

def test_correction_success_path_end_to_end():
    """Invalid draft -> verifier rejects -> issues injected -> corrected ->
    verifier accepts -> final result contains the corrected explanation."""
    fake = _success_scenario_fake()
    result = _run(fake)

    # Exactly extract + risk + 2x(explain+verify) — one correction attempt:
    assert fake.call_count == 6
    assert _count_calls(fake, "Explain these lab results") == 2
    assert _count_calls(fake, "You are a safety verifier") == 2

    # Final pipeline result contains the CORRECTED explanation:
    assert result["verified"] is True
    assert result["issues"] == []
    assert len(result["explanations"]) == 1
    final = result["explanations"][0]
    FinalExplanation.model_validate(final)
    assert final["explanation"] == CORRECTED_EXPLANATION_ITEMS[0]["explanation"]
    assert "You have diabetes" not in final["explanation"]

    # Trusted citation survives; the rogue LLM citation never leaks:
    assert final["citation"] == TRUSTED_CITATION
    assert "evil.example" not in json.dumps(result["explanations"])

    # First generation had NO correction section; the retry carries the
    # verifier's issues verbatim — proof the rejection drove the redo:
    ctx1, corr1 = _explain_prompt_parts(fake.calls[2]["input"])
    ctx2, corr2 = _explain_prompt_parts(fake.calls[4]["input"])
    assert corr1 == ""
    assert "CORRECTION REQUESTED" in corr2
    assert "[Glucose] Contains diagnostic language" in corr2

    # The structured lab/risk data handed to both generations is identical:
    assert json.loads(ctx1) == json.loads(ctx2)
    ctx = json.loads(ctx1)
    assert ctx[0]["value"] == 92.0 and ctx[0]["loinc_code"] == "2345-7"
    print("  PASS: invalid draft corrected -> re-verified -> verified=True")


# ---------------------------------------------------------------------------
# 2. Persistent failure -> bounded retry -> controlled failure
# ---------------------------------------------------------------------------

def test_persistent_verifier_failure_is_controlled():
    """First explanation fails, correction fails too -> the retry limit is
    reached -> clear controlled failure, never silent acceptance."""
    assert MAX_RETRIES == 1, "shipped retry bound must stay bounded"
    assert orch.MAX_RETRIES == MAX_RETRIES
    fake = _always_failing_fake(attempts=MAX_RETRIES + 1)
    result = _run(fake)

    # The bound held: exactly MAX_RETRIES + 1 attempts, no third attempt
    # (FakeLLM would also raise on any extra call — no responses left):
    assert fake.call_count == 2 + 2 * (MAX_RETRIES + 1)
    assert _count_calls(fake, "Explain these lab results") == MAX_RETRIES + 1
    assert _count_calls(fake, "You are a safety verifier") == MAX_RETRIES + 1

    # Controlled failure — the invalid explanation is NOT accepted:
    assert result["verified"] is False
    assert any("diagnostic language" in issue for issue in result["issues"])

    # The correction attempt WAS made, with the issues injected:
    _, corr2 = _explain_prompt_parts(fake.calls[4]["input"])
    assert "CORRECTION REQUESTED" in corr2
    assert "[Glucose] Contains diagnostic language" in corr2

    # The invalid explanation is surfaced only as explicitly unverified
    # output alongside the issues — never as a passing result:
    explanations = [FinalExplanation.model_validate(e) for e in result["explanations"]]
    assert "You have diabetes" in explanations[0].explanation
    print("  PASS: persistent failure -> controlled failure after bounded retries")


# ---------------------------------------------------------------------------
# 3. Retry limit is respected (0 and raised bounds)
# ---------------------------------------------------------------------------

def test_retry_limit_respected_for_raised_bound():
    """With MAX_RETRIES raised to 2, exactly 3 attempts happen, then stop."""
    with patch.object(orch, "MAX_RETRIES", 2):
        fake = _always_failing_fake(attempts=3)
        result = _run(fake)

    # Exactly the bound — an unbounded loop would make FakeLLM raise on the
    # extra call (no programmed responses left):
    assert fake.call_count == 2 + 2 * 3
    assert _count_calls(fake, "Explain these lab results") == 3
    assert _count_calls(fake, "You are a safety verifier") == 3
    assert result["verified"] is False
    # The 2nd and 3rd generations both carried correction issues:
    for idx in (4, 6):
        _, corr = _explain_prompt_parts(fake.calls[idx]["input"])
        assert "CORRECTION REQUESTED" in corr
    print("  PASS: raised retry bound respected — exactly MAX_RETRIES + 1 attempts")


def test_retry_limit_zero_makes_single_attempt():
    """With MAX_RETRIES = 0 there is no correction attempt at all."""
    with patch.object(orch, "MAX_RETRIES", 0):
        fake = _always_failing_fake(attempts=1)
        result = _run(fake)

    assert fake.call_count == 4
    assert _count_calls(fake, "Explain these lab results") == 1
    assert _count_calls(fake, "You are a safety verifier") == 1
    _, corr1 = _explain_prompt_parts(fake.calls[2]["input"])
    assert corr1 == ""  # no correction prompt on the single attempt
    assert result["verified"] is False
    assert result["issues"]
    print("  PASS: zero retry bound -> single attempt, controlled failure")


# ---------------------------------------------------------------------------
# 4. Safety invariants of the correction mechanism
# ---------------------------------------------------------------------------

def test_correction_prompt_restates_rules_and_preserves_data():
    """Correction can only add the verifier's issues — nothing is weakened."""
    fake = _success_scenario_fake()
    result = _run(fake)

    _, corr2 = _explain_prompt_parts(fake.calls[4]["input"])
    # Rules are restated, never relaxed:
    assert "These rules do NOT change" in corr2
    assert "Never state or imply a diagnosis" in corr2
    assert "Do not change any test_name, value, unit, or risk classification" in corr2
    assert "never invent or modify" in corr2

    # Risk classifications handed to the verifier are identical before and
    # after correction (no status was flipped to force a pass):
    _, risk_before = _verifier_payloads(fake.calls[3])
    _, risk_after = _verifier_payloads(fake.calls[5])
    assert risk_before == risk_after
    assert risk_before[0]["status"] == "normal"
    assert risk_before[0]["value"] == 92.0

    # The verifier judged the corrected output with the same rules and only
    # then approved it:
    expl_after, _ = _verifier_payloads(fake.calls[5])
    assert expl_after[0]["explanation"] == CORRECTED_EXPLANATION_ITEMS[0]["explanation"]
    assert result["verified"] is True
    print("  PASS: correction injects issues only — data, statuses, rules intact")


if __name__ == "__main__":
    print("=== P1-T2 Verifier Self-Correction ===\n")
    test_correction_success_path_end_to_end()
    test_persistent_verifier_failure_is_controlled()
    test_retry_limit_respected_for_raised_bound()
    test_retry_limit_zero_makes_single_attempt()
    test_correction_prompt_restates_rules_and_preserves_data()
    print("\nAll P1-T2 self-correction tests passed.")
