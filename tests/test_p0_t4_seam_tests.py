"""P0-T4 — Agent Seam / Schema Integration Tests.

Deterministic, fully offline (all LLM/network calls mocked). Proves every agent
boundary exchanges exactly the data its schema promises, and that malformed or
incompatible data fails safely instead of silently crossing a seam.

Seam map (actual architecture):

    PDF file
     -> tools/pdf_extractor.pdf_to_text()                -> str
     -> agents/extraction.extract_lab_values()   [LLM#1] -> List[ExtractedLabValue]
     -> agents/reference_range.lookup_ranges()   [tool]   -> List[RangeCheckedValue]
     -> agents/risk_flagger.classify_risk()      [LLM#2]  -> List[RiskFlaggedValue]
     -> agents/explainer.explain()               [LLM#3]  -> List[FinalExplanation]
     -> agents.verifier.verify()            [regex+LLM#4] -> VerifierResult
     -> pipeline/orchestrator.run_pipeline()             -> dict (final output)
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import (
    ExtractedLabValue,
    FinalExplanation,
    RangeCheckedValue,
    RiskFlaggedValue,
    VerifierResult,
)
from agents.extraction import extract_lab_values
from agents.explainer import explain
from agents.reference_range import lookup_ranges
from agents.risk_flagger import classify_risk
from agents.verifier import verify
from tools.pdf_extractor import pdf_to_text

SAMPLE_PDF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "samples", "normal_report.pdf")


# ============================================================
# Helpers — LLM is always mocked; never any real API/network call
# ============================================================

def _llm_returning_text(text):
    """call_model mock returning `text` as the model's response."""
    result = MagicMock()
    result.get_text = AsyncMock(return_value=text)
    return MagicMock(return_value=result)


def _llm_returning_items(items):
    return _llm_returning_text(json.dumps(items))


def _forbidden_llm(what="LLM"):
    """call_model mock that FAILS the test if it is ever invoked."""
    return MagicMock(side_effect=AssertionError(f"{what} must not be called"))


def _payload_after(mock_call, marker):
    """Parse the JSON context the agent embedded after `marker` in its prompt."""
    prompt = mock_call.call_args[0][1]["input"]
    return json.loads(prompt.split(marker, 1)[1])


def _verifier_payloads(mock_call):
    """Parse (explanations, risk_classifications) from the verifier prompt."""
    prompt = mock_call.call_args[0][1]["input"]
    expl_part = prompt.split("Explanations:\n", 1)[1].split("\n\nRisk classifications:\n")[0]
    risk_part = prompt.split("Risk classifications:\n", 1)[1]
    return json.loads(expl_part), json.loads(risk_part)


MOCK_RISK_ITEM = {
    "test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
    "status": "normal", "reasoning": "92 is between 70 and 100",
}
MOCK_EXPLANATION = {
    "test_name": "Glucose",
    "explanation": "Glucose measures blood sugar levels; results in this range are generally considered normal.",
    "doctor_questions": ["What is my target range?"],
    "citation": "Lab Reference (https://example.org/lab)",
}
MOCK_VERIFIER_PASS = json.dumps({"passed": True, "issues_found": []})

# ============================================================
# Step 3 — PDF extraction and Extraction agent -> ExtractedLabValue seam
# ============================================================

def test_pdf_extraction_seam_returns_text():
    """PDF extraction seam: a real sample PDF yields non-empty str for the LLM step."""
    if not os.path.exists(SAMPLE_PDF):
        pytest.skip("sample PDF not present")
    text = pdf_to_text(SAMPLE_PDF)
    assert isinstance(text, str)
    assert text.strip(), "PDF extraction must return non-empty text for the seam"


def test_extraction_agent_produces_valid_extracted_values():
    """Extraction agent output validates into ExtractedLabValue objects."""
    llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"},
        {"test_name": "Hemoglobin", "loinc_code": "unknown", "value": 14.0, "unit": "g/dL"},
    ])
    with patch("agents.extraction.call_model", llm), \
         patch("agents.extraction.get_client", MagicMock(return_value=MagicMock())):
        values = asyncio.run(extract_lab_values("synthetic report text"))

    assert len(values) == 2
    for v in values:
        assert isinstance(v, ExtractedLabValue)
        assert isinstance(v.test_name, str) and v.test_name
        assert isinstance(v.loinc_code, str) and v.loinc_code
        assert isinstance(v.value, float)
        assert isinstance(v.unit, str) and v.unit
    # Existing LOINC resolution behavior preserved (unknown -> resolved by name):
    assert values[0].loinc_code == "2345-7"
    assert values[1].loinc_code == "718-7"
    print("  PASS: extraction agent produces valid ExtractedLabValue objects")


def test_extraction_missing_required_field_rejected():
    """A missing required field must fail validation, not create an incomplete value."""
    with pytest.raises(ValidationError):
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0)  # no unit
    with pytest.raises(ValidationError):
        ExtractedLabValue(loinc_code="2345-7", value=92.0, unit="mg/dL")  # no test_name
    print("  PASS: missing required field rejected by schema")


def test_extraction_wrong_field_type_rejected():
    """Wrong field types are rejected; the schema is not weakened."""
    with pytest.raises(ValidationError):
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                          value="not-a-number", unit="mg/dL")
    with pytest.raises(ValidationError):
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                          value=None, unit="mg/dL")
    print("  PASS: wrong field types rejected by schema")


def test_extraction_malformed_llm_output_fails_controlled():
    """The extraction agent must not silently accept an incomplete LLM item."""
    llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0},  # missing unit
    ])
    with patch("agents.extraction.call_model", llm), \
         patch("agents.extraction.get_client", MagicMock(return_value=MagicMock())):
        with pytest.raises(ValidationError):
            asyncio.run(extract_lab_values("synthetic report text"))
    print("  PASS: malformed extraction LLM output fails with validation error")


def test_extraction_unparseable_llm_output_fails_controlled():
    """Non-JSON extraction output raises a controlled parsing error."""
    llm = _llm_returning_text("sorry, I cannot produce JSON right now")
    with patch("agents.extraction.call_model", llm), \
         patch("agents.extraction.get_client", MagicMock(return_value=MagicMock())):
        with pytest.raises(ValueError):
            asyncio.run(extract_lab_values("synthetic report text"))
    print("  PASS: unparseable extraction LLM output fails with controlled error")


# ============================================================
# Step 4 — Lab value -> reference lookup seam
# ============================================================

def test_lookup_seam_valid_range_preserves_identity():
    """List[ExtractedLabValue] -> lookup_ranges() -> List[RangeCheckedValue]."""
    inputs = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                                value=92.0, unit="mg/dL")]
    results = lookup_ranges(inputs)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, RangeCheckedValue)
    # Identity fields pass through unchanged:
    assert r.test_name == "Glucose"
    assert r.loinc_code == "2345-7"
    assert r.value == 92.0
    assert r.unit == "mg/dL"
    # Range fields populated from reference data:
    assert r.reference_low == 70.0
    assert r.reference_high == 100.0
    assert r.range_available is True
    assert r.in_range is True
    assert r.range_note == ""
    print("  PASS: lookup seam preserves identity and returns available range")


def test_lookup_seam_unavailable_range():
    """Unknown LOINC -> RangeCheckedValue with None/None/False (P0-T1/T2 contract)."""
    inputs = [ExtractedLabValue(test_name="Mystery", loinc_code="99999-9",
                                value=42.0, unit="U")]
    results = lookup_ranges(inputs)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, RangeCheckedValue)
    assert r.test_name == "Mystery" and r.loinc_code == "99999-9"
    assert r.reference_low is None
    assert r.reference_high is None
    assert r.range_available is False
    assert r.in_range is False
    assert r.range_note, "unavailable range must carry an explanatory note"
    print("  PASS: lookup seam returns controlled unavailable range")


# ============================================================
# Step 5 — RangeCheckedValue -> risk flagger seam
# ============================================================

def test_risk_seam_normal_value():
    """Glucose 92 within 70-100 -> RiskFlaggedValue with status='normal'."""
    checked = lookup_ranges([ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                                               value=92.0, unit="mg/dL")])
    llm = _llm_returning_items([MOCK_RISK_ITEM])
    with patch("agents.risk_flagger.call_model", llm), \
         patch("agents.risk_flagger.get_client", MagicMock(return_value=MagicMock())):
        results = asyncio.run(classify_risk(checked))

    assert len(results) == 1
    r = results[0]
    assert isinstance(r, RiskFlaggedValue)
    assert r.status == "normal"
    assert r.test_name == "Glucose" and r.loinc_code == "2345-7"
    assert r.value == 92.0 and r.unit == "mg/dL"
    assert r.reasoning
    # The range was actually presented to the LLM at this seam:
    prompt = llm.call_args[0][1]["input"]
    assert '"reference_low": 70.0' in prompt and '"reference_high": 100.0' in prompt
    print("  PASS: normal value seam -> RiskFlaggedValue(status=normal)")


def test_risk_seam_abnormal_values():
    """Values outside the project's reference range get abnormal statuses.

    Statuses follow the risk-flagger prompt's own documented rules (the mocked
    LLM behaves exactly as its prompt instructs — no new thresholds invented):
    105 -> slightly outside (<20% beyond 100) -> mildly_abnormal
    200 -> far outside (>20% beyond 100)     -> critical
    """
    checked = lookup_ranges([
        ExtractedLabValue(test_name="GlucoseHigh", loinc_code="2345-7", value=105.0, unit="mg/dL"),
        ExtractedLabValue(test_name="GlucoseVeryHigh", loinc_code="2345-7", value=200.0, unit="mg/dL"),
    ])
    assert checked[0].range_available is True and checked[0].in_range is False
    assert checked[1].range_available is True and checked[1].in_range is False

    llm = _llm_returning_items([
        {"test_name": "GlucoseHigh", "loinc_code": "2345-7", "value": 105.0, "unit": "mg/dL",
         "status": "mildly_abnormal", "reasoning": "105 is 5 above the 70-100 range"},
        {"test_name": "GlucoseVeryHigh", "loinc_code": "2345-7", "value": 200.0, "unit": "mg/dL",
         "status": "critical", "reasoning": "200 is far above the 70-100 range"},
    ])
    with patch("agents.risk_flagger.call_model", llm), \
         patch("agents.risk_flagger.get_client", MagicMock(return_value=MagicMock())):
        results = asyncio.run(classify_risk(checked))

    by_name = {r.test_name: r for r in results}
    assert by_name["GlucoseHigh"].status == "mildly_abnormal"
    assert by_name["GlucoseVeryHigh"].status == "critical"
    for r in results:
        assert isinstance(r, RiskFlaggedValue)
        assert r.status in ("mildly_abnormal", "critical")
    print("  PASS: abnormal values seam -> RiskFlaggedValue(mildly_abnormal/critical)")


def test_risk_seam_unavailable_no_llm():
    """Unavailable range at this seam: status='unavailable', LLM never called."""
    checked = lookup_ranges([ExtractedLabValue(test_name="Mystery", loinc_code="99999-9",
                                               value=42.0, unit="U")])
    assert checked[0].range_available is False

    llm = _forbidden_llm("risk classification LLM")
    with patch("agents.risk_flagger.call_model", llm), \
         patch("agents.risk_flagger.get_client",
               MagicMock(side_effect=AssertionError("client must not be created"))):
        results = asyncio.run(classify_risk(checked))

    llm.assert_not_called()
    r = results[0]
    assert isinstance(r, RiskFlaggedValue)
    assert r.status == "unavailable"
    assert r.status not in ("normal", "mildly_abnormal", "critical")
    assert r.reasoning
    print("  PASS: unavailable seam stays unavailable, LLM not called")


# ============================================================
# Step 6 — RiskFlaggedValue -> explainer seam
# ============================================================

def test_explainer_seam_valid_input():
    """Valid RiskFlaggedValue -> explain() -> List[FinalExplanation] (LLM mocked)."""
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_items([MOCK_EXPLANATION])
    with patch("agents.explainer.call_model", llm), \
         patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: f"Patched citation for {name}")):
        explanations = asyncio.run(explain(risk_values))

    assert len(explanations) == 1
    e = explanations[0]
    assert isinstance(e, FinalExplanation)
    assert e.test_name == "Glucose"
    assert e.explanation
    assert len(e.doctor_questions) >= 1
    assert e.citation

    # The explainer's input context carries the full risk identity + status:
    context = _payload_after(llm, "Explain these lab results:\n")
    assert context[0]["test_name"] == "Glucose"
    assert context[0]["loinc_code"] == "2345-7"
    assert context[0]["value"] == 92.0
    assert context[0]["unit"] == "mg/dL"
    assert context[0]["status"] == "normal"
    assert context[0]["reasoning"]
    assert context[0]["citation"] == "Patched citation for Glucose"  # enrichment
    print("  PASS: risk -> explainer seam produces valid FinalExplanation")


def test_explainer_malformed_llm_output_fails_controlled():
    """Malformed explainer LLM output fails validation instead of partial objects."""
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_items([{"test_name": "Glucose"}])  # missing required fields
    with patch("agents.explainer.call_model", llm), \
         patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: "Patched citation")):
        with pytest.raises(ValidationError):
            asyncio.run(explain(risk_values))
    print("  PASS: malformed explainer output fails with validation error")


def test_explainer_empty_input_no_llm_no_invention():
    """Empty risk list -> empty explanations, WITHOUT calling the LLM.

    A model asked to explain zero values could hallucinate explanations for
    values that do not exist; empty input must short-circuit.
    """
    llm = _forbidden_llm("explainer LLM")
    with patch("agents.explainer.call_model", llm), \
         patch("agents.explainer.get_client",
               MagicMock(side_effect=AssertionError("client must not be created"))), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=AssertionError("citation lookup must not run"))):
        explanations = asyncio.run(explain([]))

    assert explanations == []
    llm.assert_not_called()
    print("  PASS: empty explainer input returns [] without calling the LLM")


# ============================================================
# Steps 7-8 — FinalExplanation + RiskFlaggedValue -> verifier seam
# ============================================================

def test_verifier_seam_valid_explanations():
    """Valid explanations + risk values -> VerifierResult consumed by orchestrator."""
    explanations = [FinalExplanation(**MOCK_EXPLANATION)]
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_text(MOCK_VERIFIER_PASS)
    with patch("agents.verifier.call_model", llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        result = asyncio.run(verify(explanations, risk_values))

    assert isinstance(result, VerifierResult)
    assert result.passed is True
    assert result.action == "approve"
    assert result.issues_found == []
    # Both payloads reached the verifier in schema-valid form:
    expl_payload, risk_payload = _verifier_payloads(llm)
    expl = [FinalExplanation.model_validate(e) for e in expl_payload]
    risk = [RiskFlaggedValue.model_validate(r) for r in risk_payload]
    assert expl[0].test_name == "Glucose"
    assert risk[0].loinc_code == "2345-7" and risk[0].status == "normal"
    print("  PASS: explainer -> verifier seam produces valid VerifierResult")


def test_verifier_code_check_overrides_llm_pass():
    """Deterministic code check catches diagnostic language even if LLM says pass."""
    explanations = [FinalExplanation(
        test_name="Glucose",
        explanation="You have diabetes based on this result.",
        doctor_questions=["Should I retest?"],
        citation="Lab Reference (https://example.org/lab)",
    )]
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_text(MOCK_VERIFIER_PASS)  # LLM claims everything is fine
    with patch("agents.verifier.call_model", llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        result = asyncio.run(verify(explanations, risk_values))

    assert isinstance(result, VerifierResult)
    assert result.passed is False
    assert result.action == "send_back_for_correction"
    assert any("diagnostic language" in i for i in result.issues_found)
    print("  PASS: verifier code check detects diagnostic language deterministically")


def test_verifier_malformed_output_missing_field_fails_closed():
    """Malformed verifier LLM output (missing issues_found) must NOT approve.

    e.g. {"approved": true} is not the contracted schema — the system must
    fail closed for review instead of silently treating it as valid approval.
    """
    explanations = [FinalExplanation(**MOCK_EXPLANATION)]
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_text(json.dumps({"approved": True}))
    with patch("agents.verifier.call_model", llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        result = asyncio.run(verify(explanations, risk_values))

    assert isinstance(result, VerifierResult)
    assert result.passed is False, "malformed verifier output must never silently approve"
    assert result.action == "send_back_for_correction"
    assert result.issues_found
    print("  PASS: malformed verifier output (missing field) fails closed")


def test_verifier_malformed_output_wrong_type_fails_closed():
    """Wrong field types in verifier LLM output must NOT approve."""
    explanations = [FinalExplanation(**MOCK_EXPLANATION)]
    risk_values = [RiskFlaggedValue(**MOCK_RISK_ITEM)]
    llm = _llm_returning_text(json.dumps({"passed": "not-a-boolean", "issues_found": []}))
    with patch("agents.verifier.call_model", llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        result = asyncio.run(verify(explanations, risk_values))

    assert isinstance(result, VerifierResult)
    assert result.passed is False, "wrong-typed verifier output must never silently approve"
    assert result.action == "send_back_for_correction"
    assert result.issues_found
    print("  PASS: malformed verifier output (wrong type) fails closed")


# ============================================================
# Step 9 — Schema serialization between agents
# ============================================================

def test_serialization_roundtrip_all_schemas():
    """Pydantic -> JSON -> Pydantic preserves every seam-critical field."""
    samples = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        RangeCheckedValue(test_name="Mystery", loinc_code="99999-9", value=42.0, unit="U",
                          reference_low=None, reference_high=None, in_range=False,
                          range_available=False, range_note="LOINC unknown"),
        RiskFlaggedValue(test_name="Mystery", loinc_code="99999-9", value=42.0, unit="U",
                         status="unavailable", reasoning="No valid reference range."),
        FinalExplanation(test_name="Glucose",
                         explanation="Glucose measures blood sugar levels.",
                         doctor_questions=["What is my range?"],
                         citation="Lab Reference (https://example.org/lab)"),
        VerifierResult(passed=False, issues_found=["missing citation"],
                       action="send_back_for_correction"),
    ]
    for original in samples:
        data = json.loads(json.dumps(original.model_dump()))
        restored = type(original).model_validate(data)
        assert restored.model_dump() == original.model_dump(), \
            f"{type(original).__name__} lost fields in round trip"

    # Specific critical values:
    rc = RangeCheckedValue.model_validate(json.loads(json.dumps(samples[1].model_dump())))
    assert rc.reference_low is None and rc.reference_high is None  # None never -> 0
    assert rc.range_available is False                             # False never -> True
    assert rc.range_note == "LOINC unknown"
    rf = RiskFlaggedValue.model_validate(json.loads(json.dumps(samples[2].model_dump())))
    assert rf.status == "unavailable"
    fe = FinalExplanation.model_validate(json.loads(json.dumps(samples[3].model_dump())))
    assert fe.citation == samples[3].citation
    assert fe.doctor_questions == ["What is my range?"]
    print("  PASS: all five schemas survive serialization round trips")


def test_serialization_rejects_invalid_values():
    """Invalid serialized data is rejected, not coerced into validity."""
    with pytest.raises(ValidationError):  # invalid status Literal
        RiskFlaggedValue.model_validate({"test_name": "T", "value": 1.0, "unit": "U",
                                         "status": "severe", "reasoning": "r"})
    with pytest.raises(ValidationError):  # missing required field
        FinalExplanation.model_validate({"test_name": "T"})
    with pytest.raises(ValidationError):  # unparseable string for bool
        # (Pydantic lax mode legitimately coerces "yes"/"no"/"true"/"false",
        # so a genuinely invalid value is used here)
        VerifierResult.model_validate({"passed": "not-a-boolean",
                                       "issues_found": [], "action": "approve"})
    print("  PASS: invalid serialized data rejected by schemas")


# ============================================================
# Step 10 — Wrong-schema crossing
# ============================================================

def test_wrong_schema_crossing_rejected():
    """An object from one stage cannot silently become another stage's schema."""
    extracted = ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                                  value=92.0, unit="mg/dL")
    checked = RangeCheckedValue(test_name="Glucose", loinc_code="2345-7",
                                value=92.0, unit="mg/dL",
                                reference_low=70.0, reference_high=100.0,
                                in_range=True, range_available=True)
    risk = RiskFlaggedValue(**MOCK_RISK_ITEM)
    explanation = FinalExplanation(**MOCK_EXPLANATION)
    verifier = VerifierResult(passed=True, issues_found=[], action="approve")

    # ExtractedLabValue -> RiskFlaggedValue (missing status/reasoning):
    with pytest.raises(ValidationError):
        RiskFlaggedValue.model_validate(extracted.model_dump())
    # RangeCheckedValue -> FinalExplanation (missing explanation/questions/citation):
    with pytest.raises(ValidationError):
        FinalExplanation.model_validate(checked.model_dump())
    # RiskFlaggedValue -> FinalExplanation (missing explanation/questions/citation):
    with pytest.raises(ValidationError):
        FinalExplanation.model_validate(risk.model_dump())
    # FinalExplanation -> VerifierResult (missing passed/issues/action):
    with pytest.raises(ValidationError):
        VerifierResult.model_validate(explanation.model_dump())
    # VerifierResult -> RiskFlaggedValue (missing test_name/value/unit/status/reasoning):
    with pytest.raises(ValidationError):
        RiskFlaggedValue.model_validate(verifier.model_dump())
    print("  PASS: wrong-schema cross-stage data rejected")


# ============================================================
# Steps 11-12 — Identity preservation (test_name/loinc/value/unit)
# ============================================================

def test_identity_preserved_through_full_chain():
    """Lab identity survives ExtractedLabValue -> RangeChecked -> Risk -> Explainer."""
    extracted = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7",
                                   value=92.0, unit="mg/dL")]
    checked = lookup_ranges(extracted)
    assert (checked[0].test_name, checked[0].loinc_code,
            checked[0].value, checked[0].unit) == ("Glucose", "2345-7", 92.0, "mg/dL")

    risk_llm = _llm_returning_items([MOCK_RISK_ITEM])
    with patch("agents.risk_flagger.call_model", risk_llm), \
         patch("agents.risk_flagger.get_client", MagicMock(return_value=MagicMock())):
        risk = asyncio.run(classify_risk(checked))
    assert (risk[0].test_name, risk[0].loinc_code,
            risk[0].value, risk[0].unit) == ("Glucose", "2345-7", 92.0, "mg/dL")

    explainer_llm = _llm_returning_items([MOCK_EXPLANATION])
    with patch("agents.explainer.call_model", explainer_llm), \
         patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: "Patched citation")):
        explanations = asyncio.run(explain(risk))
    # FinalExplanation carries test_name (its schema's identity field):
    assert explanations[0].test_name == "Glucose"
    # The explainer context still carried the full identity:
    ctx = _payload_after(explainer_llm, "Explain these lab results:\n")
    assert (ctx[0]["test_name"], ctx[0]["loinc_code"],
            ctx[0]["value"], ctx[0]["unit"]) == ("Glucose", "2345-7", 92.0, "mg/dL")
    print("  PASS: lab identity preserved through every stage")


# ============================================================
# Step 13 — Unavailable range through the whole pipeline
# ============================================================

def test_unavailable_survives_every_seam():
    """Unknown LOINC stays unavailable from extraction to verifier."""
    # Extraction (LLM mocked) produces an unknown LOINC:
    extract_llm = _llm_returning_items([
        {"test_name": "Mystery", "loinc_code": "99999-9", "value": 42.0, "unit": "U"},
    ])
    with patch("agents.extraction.call_model", extract_llm), \
         patch("agents.extraction.get_client", MagicMock(return_value=MagicMock())):
        extracted = asyncio.run(extract_lab_values("synthetic report text"))
    assert extracted[0].loinc_code == "99999-9"

    # Lookup: unavailable by construction (P0-T1 contract):
    checked = lookup_ranges(extracted)
    assert checked[0].range_available is False
    assert checked[0].reference_low is None and checked[0].reference_high is None

    # Risk flagger: no LLM, status unavailable:
    risk_llm = _forbidden_llm("risk classification LLM")
    with patch("agents.risk_flagger.call_model", risk_llm), \
         patch("agents.risk_flagger.get_client",
               MagicMock(side_effect=AssertionError("client must not be created"))):
        risk = asyncio.run(classify_risk(checked))
    risk_llm.assert_not_called()
    assert risk[0].status == "unavailable"
    assert risk[0].status not in ("normal", "mildly_abnormal", "critical")

    # Explainer receives (and reports) the unavailable status:
    explainer_llm = _llm_returning_items([{
        "test_name": "Mystery",
        "explanation": "This test could not be assessed; discuss the result with your doctor.",
        "doctor_questions": ["Can this test be repeated?"],
        "citation": "Lab Reference (https://example.org/lab)",
    }])
    with patch("agents.explainer.call_model", explainer_llm), \
         patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())), \
         patch("agents.explainer.get_citation",
               MagicMock(side_effect=lambda loinc, name: "Patched citation")):
        explanations = asyncio.run(explain(risk))
    ctx = _payload_after(explainer_llm, "Explain these lab results:\n")
    assert ctx[0]["status"] == "unavailable"
    assert ctx[0]["loinc_code"] == "99999-9"
    assert explanations[0].test_name == "Mystery"

    # Verifier receives the unavailable status too:
    verifier_llm = _llm_returning_text(MOCK_VERIFIER_PASS)
    with patch("agents.verifier.call_model", verifier_llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        verdict = asyncio.run(verify(explanations, risk))
    _, risk_payload = _verifier_payloads(verifier_llm)
    assert risk_payload[0]["status"] == "unavailable"
    assert verdict.passed is True  # controlled explanation of an unavailable value is fine
    print("  PASS: unavailable range survives every seam unchanged")


# ============================================================
# Step 15 — Mixed batch: no cross-contamination
# ============================================================

def test_mixed_batch_no_cross_contamination():
    """Five values with distinct identities keep their own identity and status."""
    inputs = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=200.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL"),
        ExtractedLabValue(test_name="GlucoseMmol", loinc_code="2345-7", value=5.1, unit="mmol/L"),
        ExtractedLabValue(test_name="Mystery", loinc_code="99999-9", value=42.0, unit="U"),
    ]
    checked = lookup_ranges(inputs)
    # Two Glucose entries: both classifiable ranges (one normal, one abnormal):
    assert checked[0].range_available is True
    assert checked[1].range_available is True
    # Hemoglobin (sex-specific), unit mismatch, unknown LOINC: unavailable:
    assert checked[2].range_available is False
    assert checked[3].range_available is False
    assert checked[4].range_available is False

    risk_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
         "status": "normal", "reasoning": "92 is between 70 and 100"},
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 200.0, "unit": "mg/dL",
         "status": "critical", "reasoning": "200 is far above 70-100"},
    ])
    with patch("agents.risk_flagger.call_model", risk_llm), \
         patch("agents.risk_flagger.get_client", MagicMock(return_value=MagicMock())):
        risk = asyncio.run(classify_risk(checked))

    assert len(risk) == 5
    # Identity -> status keyed by (test_name, value): no value inherits another
    # value's range, status, or LOINC:
    by_identity = {(r.test_name, r.value): r for r in risk}
    assert by_identity[("Glucose", 92.0)].status == "normal"
    assert by_identity[("Glucose", 200.0)].status == "critical"   # same name, own status
    assert by_identity[("Hemoglobin", 14.0)].status == "unavailable"
    assert by_identity[("GlucoseMmol", 5.1)].status == "unavailable"
    assert by_identity[("Mystery", 42.0)].status == "unavailable"
    # Own LOINCs and per-value explanatory notes:
    assert by_identity[("Hemoglobin", 14.0)].loinc_code == "718-7"
    assert "Sex-specific" in by_identity[("Hemoglobin", 14.0)].reasoning
    assert "Unit mismatch" in by_identity[("GlucoseMmol", 5.1)].reasoning
    assert "not in the supported labs list" in by_identity[("Mystery", 42.0)].reasoning
    # Same-named Glucose values each kept their own value:
    assert by_identity[("Glucose", 92.0)].value == 92.0
    assert by_identity[("Glucose", 200.0)].value == 200.0
    print("  PASS: mixed batch — no cross-contamination of identity or status")


# ============================================================
# Steps 16-17 — Empty inputs and orchestrator seam
# ============================================================

def test_empty_input_seams_controlled():
    """Empty lists at agent boundaries behave safely and never invent results."""
    # Reference lookup:
    assert lookup_ranges([]) == []
    # Risk flagger: no LLM for empty input:
    risk_llm = _forbidden_llm("risk classification LLM")
    with patch("agents.risk_flagger.call_model", risk_llm), \
         patch("agents.risk_flagger.get_client",
               MagicMock(side_effect=AssertionError("client must not be created"))):
        assert asyncio.run(classify_risk([])) == []
    risk_llm.assert_not_called()
    # Verifier: controlled result for empty inputs (existing contract):
    verifier_llm = _llm_returning_text(MOCK_VERIFIER_PASS)
    with patch("agents.verifier.call_model", verifier_llm), \
         patch("agents.verifier.get_client", MagicMock(return_value=MagicMock())):
        result = asyncio.run(verify([], []))
    assert isinstance(result, VerifierResult)
    print("  PASS: empty inputs behave in a controlled way at every boundary")


def _orchestrator_env(stack, *, pdf_text="synthetic report text", pdf_error=None,
                      extract_llm=None, risk_llm=None, explainer_llm=None,
                      verifier_llm=None):
    """Wire mocked LLM seams into the REAL orchestrator.

    llm=None -> that LLM must never be called (test fails if it is).
    Returns (orchestrator_module, {name: call_model_mock}).
    """
    import pipeline.orchestrator as orch

    if pdf_error is not None:
        stack.enter_context(patch.object(orch, "pdf_to_text", side_effect=pdf_error))
    else:
        stack.enter_context(patch.object(orch, "pdf_to_text", return_value=pdf_text))

    def _wire(module, llm, name):
        m = llm if llm is not None else _forbidden_llm(name)
        stack.enter_context(patch(f"{module}.call_model", m))
        stack.enter_context(patch(f"{module}.get_client", MagicMock(return_value=MagicMock())))
        return m

    mocks = {
        "extract": _wire("agents.extraction", extract_llm, "extraction LLM"),
        "risk": _wire("agents.risk_flagger", risk_llm, "risk LLM"),
        "explain": _wire("agents.explainer", explainer_llm, "explainer LLM"),
        "verify": _wire("agents.verifier", verifier_llm, "verifier LLM"),
    }
    stack.enter_context(patch(
        "agents.explainer.get_citation",
        MagicMock(side_effect=lambda loinc, name: "Patched citation")))
    return orch, mocks


def test_orchestrator_full_seam_mocked_e2e():
    """Full orchestrator: every agent runs for real with only LLMs mocked."""
    from contextlib import ExitStack

    extract_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"},
    ])
    risk_llm = _llm_returning_items([MOCK_RISK_ITEM])
    explainer_llm = _llm_returning_items([MOCK_EXPLANATION])
    verifier_llm = _llm_returning_text(MOCK_VERIFIER_PASS)

    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(
            stack, extract_llm=extract_llm, risk_llm=risk_llm,
            explainer_llm=explainer_llm, verifier_llm=verifier_llm)
        result = asyncio.run(orch.run_pipeline("dummy_report.pdf"))

    # Final output schema:
    assert result["verified"] is True
    assert result["issues"] == []
    assert len(result["explanations"]) == 1
    e = result["explanations"][0]
    for key in ("test_name", "explanation", "doctor_questions", "citation"):
        assert key in e, f"final output missing {key}"

    # Boundary schemas (payloads actually sent across each seam):
    # extraction input received the raw PDF text:
    assert "synthetic report text" in mocks["extract"].call_args[0][1]["input"]
    # lookup output -> risk flagger input validates as RangeCheckedValue:
    risk_input = _payload_after(mocks["risk"], "Classify these lab values:\n")
    checked = [RangeCheckedValue.model_validate(r) for r in risk_input]
    assert checked[0].loinc_code == "2345-7" and checked[0].range_available is True
    # risk output -> explainer input validates as RiskFlaggedValue:
    expl_input = _payload_after(mocks["explain"], "Explain these lab results:\n")
    risk_values = [RiskFlaggedValue.model_validate(r) for r in expl_input]
    assert risk_values[0].status == "normal" and risk_values[0].loinc_code == "2345-7"
    # explainer output + risk values -> verifier input validate:
    expl_payload, risk_payload = _verifier_payloads(mocks["verify"])
    explanations = [FinalExplanation.model_validate(x) for x in expl_payload]
    verdict_risk = [RiskFlaggedValue.model_validate(r) for r in risk_payload]
    assert explanations[0].test_name == "Glucose"
    assert verdict_risk[0].loinc_code == "2345-7"
    print("  PASS: full orchestrator seam (all agents real, LLMs mocked)")


def test_orchestrator_verifier_failure_propagates():
    """VerifierResult(passed=False) drives orchestrator retry and failure output."""
    from contextlib import ExitStack

    extract_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"},
    ])
    risk_llm = _llm_returning_items([MOCK_RISK_ITEM])
    explainer_llm = _llm_returning_items([MOCK_EXPLANATION])
    verifier_llm = _llm_returning_text(
        json.dumps({"passed": False, "issues_found": ["missing citation"]}))

    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(
            stack, extract_llm=extract_llm, risk_llm=risk_llm,
            explainer_llm=explainer_llm, verifier_llm=verifier_llm)
        result = asyncio.run(orch.run_pipeline("dummy_report.pdf"))

    # The orchestrator consumed VerifierResult: retried once, then reported failure:
    assert result["verified"] is False
    assert result["issues"] == ["missing citation"]
    assert mocks["explain"].call_count == 2   # MAX_RETRIES + 1 attempts
    assert mocks["verify"].call_count == 2
    print("  PASS: verifier failure propagates through the orchestrator correctly")


def test_orchestrator_wrong_loinc_dropped_never_reaches_explainer():
    """P0-T2 identity protection survives orchestration: a wrong-LOINC
    classification is dropped and can never reach the explainer."""
    from contextlib import ExitStack

    extract_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"},
    ])
    # Risk LLM echoes the right test_name/value/unit but the WRONG loinc:
    risk_llm = _llm_returning_items([{
        "test_name": "Glucose", "loinc_code": "WRONG-LOINC", "value": 92.0,
        "unit": "mg/dL", "status": "critical", "reasoning": "wrong identity",
    }])
    verifier_llm = _llm_returning_text(MOCK_VERIFIER_PASS)
    # explainer_llm left None -> forbidden: must never be called.

    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(
            stack, extract_llm=extract_llm, risk_llm=risk_llm,
            verifier_llm=verifier_llm)
        result = asyncio.run(orch.run_pipeline("dummy_report.pdf"))

    # The value WAS sent to the risk LLM with its correct LOINC...
    risk_prompt = mocks["risk"].call_args[0][1]["input"]
    assert "2345-7" in risk_prompt and "WRONG-LOINC" not in risk_prompt
    # ...but the wrong-identity classification was rejected, so nothing reached
    # the explainer (empty risk list short-circuits it), and no wrong-LOINC
    # classification appears anywhere downstream:
    mocks["explain"].assert_not_called()
    assert result["explanations"] == []
    assert "WRONG-LOINC" not in json.dumps(result)
    print("  PASS: wrong-LOINC classification blocked by P0-T2 guard during orchestration")


# ============================================================
# Step 19 — Error propagation (upstream failure stops safely)
# ============================================================

def test_pdf_failure_stops_before_extraction():
    """PDF extraction failure must stop the pipeline before any LLM is used."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(stack, pdf_error=RuntimeError("corrupt PDF"))
        with pytest.raises(RuntimeError):
            asyncio.run(orch.run_pipeline("broken.pdf"))

    mocks["extract"].assert_not_called()
    mocks["risk"].assert_not_called()
    mocks["explain"].assert_not_called()
    mocks["verify"].assert_not_called()
    print("  PASS: PDF failure stops the pipeline before lab extraction")


def test_empty_pdf_text_stops_before_extraction():
    """Empty PDF text is a controlled failure; extraction LLM is never called."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(stack, pdf_text="   ")
        with pytest.raises(ValueError):
            asyncio.run(orch.run_pipeline("blank.pdf"))

    mocks["extract"].assert_not_called()
    mocks["risk"].assert_not_called()
    print("  PASS: empty PDF text stops the pipeline before lab extraction")


def test_malformed_extraction_stops_before_downstream():
    """An invalid ExtractedLabValue from extraction stops the pipeline; no
    downstream agent processes the invalid data."""
    from contextlib import ExitStack

    extract_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0},  # missing unit
    ])
    with ExitStack() as stack:
        orch, mocks = _orchestrator_env(stack, extract_llm=extract_llm)
        with pytest.raises(ValidationError):
            asyncio.run(orch.run_pipeline("dummy_report.pdf"))

    mocks["risk"].assert_not_called()
    mocks["explain"].assert_not_called()
    mocks["verify"].assert_not_called()
    print("  PASS: malformed extraction output stops the pipeline safely")


if __name__ == "__main__":
    print("=== P0-T4 Agent Seam / Schema Integration Tests ===\n")
    test_pdf_extraction_seam_returns_text()
    test_extraction_agent_produces_valid_extracted_values()
    test_extraction_missing_required_field_rejected()
    test_extraction_wrong_field_type_rejected()
    test_extraction_malformed_llm_output_fails_controlled()
    test_extraction_unparseable_llm_output_fails_controlled()
    test_lookup_seam_valid_range_preserves_identity()
    test_lookup_seam_unavailable_range()
    test_risk_seam_normal_value()
    test_risk_seam_abnormal_values()
    test_risk_seam_unavailable_no_llm()
    test_explainer_seam_valid_input()
    test_explainer_malformed_llm_output_fails_controlled()
    test_explainer_empty_input_no_llm_no_invention()
    test_verifier_seam_valid_explanations()
    test_verifier_code_check_overrides_llm_pass()
    test_verifier_malformed_output_missing_field_fails_closed()
    test_verifier_malformed_output_wrong_type_fails_closed()
    test_serialization_roundtrip_all_schemas()
    test_serialization_rejects_invalid_values()
    test_wrong_schema_crossing_rejected()
    test_identity_preserved_through_full_chain()
    test_unavailable_survives_every_seam()
    test_mixed_batch_no_cross_contamination()
    test_empty_input_seams_controlled()
    test_orchestrator_full_seam_mocked_e2e()
    test_orchestrator_verifier_failure_propagates()
    test_orchestrator_wrong_loinc_dropped_never_reaches_explainer()
    test_pdf_failure_stops_before_extraction()
    test_empty_pdf_text_stops_before_extraction()
    test_malformed_extraction_stops_before_downstream()
    print("\nAll P0-T4 seam tests passed.")
