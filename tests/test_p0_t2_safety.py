"""P0-T2 — Reference-Range Safety Consumer Audit regression tests.

Central safety invariant proven by these tests (all deterministic, no external API):

    If range_available == False, the system must NEVER classify the value as
    normal / mildly_abnormal / critical based on a reference range. It must stay:

        reference_low  = None
        reference_high = None
        range_available = False
        status = "unavailable"

Covers:
- Case A: unknown LOINC -> unavailable
- Case B: known LOINC, missing sex/context -> unavailable
- Case C: unit mismatch (mmol/L vs mg/dL) -> unavailable
- None never becomes 0 (including through serialization)
- range_available=False survives serialization/deserialization
- range_available=False survives the orchestrator (non-LLM end-to-end)
- Unavailable values never reach the range-based LLM classification path
- Rogue LLM output cannot re-classify an unavailable value
- Valid ranges, unit conversions, and boundaries still behave normally
- Reference provenance (loinc_code + range_note) survives the pipeline
"""

import asyncio
import json
import os
import sys
from contextlib import ExitStack
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
from agents.reference_range import lookup_ranges
from agents.risk_flagger import classify_risk


# ============================================================
# Helpers — LLM is always mocked; never any real API call
# ============================================================

def _forbidden_call_model():
    """call_model mock that FAILS the test if it is ever invoked."""
    return MagicMock(side_effect=AssertionError(
        "call_model must NOT be invoked for values without a usable reference range"
    ))


def _call_model_returning(items):
    """call_model mock that returns `items` as the model's JSON answer."""
    mock_result = MagicMock()
    mock_result.get_text = AsyncMock(return_value=json.dumps(items))
    return MagicMock(return_value=mock_result)


def _run_classify(checked, llm_items=None):
    """Run classify_risk with a mocked LLM.

    llm_items=None  -> the LLM must never be called (test fails if it is).
    llm_items=[...] -> the mocked LLM returns these items as its answer.
    Returns (results, call_model_mock).
    """
    if llm_items is None:
        call_mock = _forbidden_call_model()
        client_mock = MagicMock(side_effect=AssertionError("LLM client must not be created"))
    else:
        call_mock = _call_model_returning(llm_items)
        client_mock = MagicMock(return_value=MagicMock())
    with patch("agents.risk_flagger.call_model", call_mock), \
         patch("agents.risk_flagger.get_client", client_mock):
        results = asyncio.run(classify_risk(checked))
    return results, call_mock


# ============================================================
# Step 4 — unavailable must survive lookup + risk flagger
# ============================================================

def test_case_a_unknown_loinc_is_unavailable():
    """Unknown LOINC -> None/None/False at lookup and status='unavailable' downstream."""
    values = [ExtractedLabValue(test_name="Mystery Test", loinc_code="99999-9", value=42.0, unit="U")]
    checked = lookup_ranges(values)
    c = checked[0]
    assert c.reference_low is None, "Unknown LOINC must not get a fake reference_low"
    assert c.reference_high is None, "Unknown LOINC must not get a fake reference_high"
    assert c.range_available is False
    assert c.in_range is False

    results, llm_mock = _run_classify(checked)  # LLM forbidden
    llm_mock.assert_not_called()
    r = results[0]
    assert r.status == "unavailable"
    assert r.status not in ("normal", "mildly_abnormal", "critical")
    assert "no range-based risk classification was performed" in r.reasoning
    assert "not in the supported labs list" in r.reasoning, "specific cause (range_note) must survive"
    print("  PASS: Case A — unknown LOINC stays unavailable, LLM never called")


def test_case_b_missing_sex_context_is_unavailable():
    """Known LOINC (718-7) with sex-specific ranges and no patient sex -> unavailable."""
    # 718-7 (Hemoglobin) has male/female-only rows in the project's reference data.
    values = [ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL")]
    checked = lookup_ranges(values)
    c = checked[0]
    assert c.reference_low is None, "Missing sex must not yield a guessed range"
    assert c.reference_high is None, "Missing sex must not yield a guessed range"
    assert c.range_available is False
    assert c.in_range is False

    results, llm_mock = _run_classify(checked)  # LLM forbidden
    llm_mock.assert_not_called()
    r = results[0]
    assert r.status == "unavailable"
    assert r.status not in ("normal", "mildly_abnormal", "critical")
    assert "Sex-specific ranges available" in r.reasoning, "specific cause (range_note) must survive"
    assert "no range-based risk classification was performed" in r.reasoning
    print("  PASS: Case B — missing sex/context stays unavailable, no range guessed")


def test_case_c_unit_mismatch_is_unavailable_and_skips_llm():
    """Unit mismatch (mmol/L vs mg/dL) -> unavailable, and never reaches the LLM."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=5.1, unit="mmol/L")]
    checked = lookup_ranges(values)
    c = checked[0]
    assert c.range_available is False, "Generic mmol/L <-> mg/dL conversion must stay rejected"
    assert c.reference_low is None
    assert c.reference_high is None
    assert c.in_range is False

    results, llm_mock = _run_classify(checked)  # LLM forbidden
    llm_mock.assert_not_called()
    r = results[0]
    assert r.status == "unavailable"
    assert r.status not in ("normal", "mildly_abnormal", "critical")
    assert "Unit mismatch" in r.reasoning
    assert "mmol/L" in r.reasoning and "mg/dL" in r.reasoning
    assert "no range-based risk classification was performed" in r.reasoning
    print("  PASS: Case C — unit mismatch stays unavailable and never reaches the LLM")


# ============================================================
# Step 2 — None never becomes 0 (incl. serialization)
# ============================================================

def test_none_reference_values_never_become_zero():
    """reference_low/high stay None through model_dump, JSON and back."""
    cases = [
        ExtractedLabValue(test_name="Mystery", loinc_code="99999-9", value=1.0, unit="U"),
        ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL"),
        ExtractedLabValue(test_name="GlucoseMmol", loinc_code="2345-7", value=5.1, unit="mmol/L"),
    ]
    checked = lookup_ranges(cases)
    assert len(checked) == 3
    for c in checked:
        # Identity check first — never 0, 0.0, "0" or "0.0"
        assert c.reference_low is None, f"{c.test_name}: reference_low became {c.reference_low!r}"
        assert c.reference_high is None, f"{c.test_name}: reference_high became {c.reference_high!r}"
        assert c.reference_low not in (0, 0.0, "0", "0.0")
        assert c.reference_high not in (0, 0.0, "0", "0.0")
        # Serialization round-trip must preserve None (json null), never 0
        dumped = json.loads(json.dumps(c.model_dump()))
        assert dumped["reference_low"] is None
        assert dumped["reference_high"] is None
        restored = RangeCheckedValue.model_validate(dumped)
        assert restored.reference_low is None
        assert restored.reference_high is None
        assert restored.range_available is False
    print("  PASS: None references never become 0 through serialization")


def test_range_available_false_survives_serialization():
    """range_available=False and status='unavailable' survive model <-> JSON round-trips."""
    v = RangeCheckedValue(
        test_name="GlucoseMmol", loinc_code="2345-7", value=5.1, unit="mmol/L",
        reference_low=None, reference_high=None, in_range=False,
        range_available=False, range_note="Unit mismatch: extracted 'mmol/L' vs reference 'mg/dL'",
    )
    restored = RangeCheckedValue.model_validate_json(v.model_dump_json())
    assert restored.range_available is False
    assert restored.in_range is False
    assert restored.reference_low is None
    assert restored.reference_high is None
    assert restored.range_note == v.range_note

    r = RiskFlaggedValue(
        test_name="GlucoseMmol", loinc_code="2345-7", value=5.1, unit="mmol/L",
        status="unavailable", reasoning="Cannot classify: unit mismatch.",
    )
    restored_r = RiskFlaggedValue.model_validate_json(r.model_dump_json())
    assert restored_r.status == "unavailable"
    assert restored_r.loinc_code == "2345-7"
    print("  PASS: range_available=False and status='unavailable' survive serialization")


# ============================================================
# Step 5 — unavailable values never reach range-based LLM classification
# ============================================================

def test_unavailable_value_not_sent_to_range_based_llm():
    """Mixed batch: only the available value is serialized into the LLM prompt."""
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="MysteryMismatch", loinc_code="2345-7", value=5.1, unit="mmol/L"),
    ]
    checked = lookup_ranges(values)
    assert checked[0].range_available is True
    assert checked[1].range_available is False  # unit mismatch

    llm_items = [{
        "test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
        "status": "normal", "reasoning": "92 is between 70 and 100",
    }]
    results, llm_mock = _run_classify(checked, llm_items=llm_items)
    llm_mock.assert_called_once()

    prompt = llm_mock.call_args[0][1]["input"]
    assert "MysteryMismatch" not in prompt, "unavailable value must NOT be sent to the LLM"
    assert prompt.count('"range_available"') == 1, "exactly one (available) value may be sent"
    assert '"reference_low": 70.0' in prompt and '"reference_high": 100.0' in prompt

    by_name = {r.test_name: r for r in results}
    assert by_name["Glucose"].status == "normal"
    assert by_name["MysteryMismatch"].status == "unavailable"
    assert by_name["MysteryMismatch"].status not in ("normal", "mildly_abnormal", "critical")
    assert "Unit mismatch" in by_name["MysteryMismatch"].reasoning
    print("  PASS: unit-mismatch value never reaches range-based LLM classification")


def test_rogue_llm_status_for_unavailable_value_is_dropped():
    """Even if the LLM invents a classification for an unavailable value, it is dropped."""
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="MysteryMismatch", loinc_code="2345-7", value=5.1, unit="mmol/L"),
    ]
    checked = lookup_ranges(values)

    llm_items = [
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
         "status": "normal", "reasoning": "in range"},
        # Rogue outputs that must be dropped — even with a loinc_code present,
        # the full identity (test_name, loinc_code, value, unit) must match a
        # value that was actually sent to the LLM:
        {"test_name": "MysteryMismatch", "loinc_code": "2345-7", "value": 5.1, "unit": "mmol/L",
         "status": "critical", "reasoning": "rogue classification"},
        {"test_name": "NotInBatch", "loinc_code": "99999-9", "value": 1.0, "unit": "U",
         "status": "normal", "reasoning": "rogue classification"},
    ]
    results, _ = _run_classify(checked, llm_items=llm_items)
    by_name = {r.test_name: r for r in results}
    assert by_name["Glucose"].status == "normal"
    assert by_name["MysteryMismatch"].status == "unavailable", \
        "range_available=False must never become critical via rogue LLM output"
    assert by_name["MysteryMismatch"].status not in ("normal", "mildly_abnormal", "critical")
    assert "NotInBatch" not in by_name, "classifications for values never sent must be dropped"
    print("  PASS: rogue LLM output cannot re-classify an unavailable value")


def test_malformed_range_states_cannot_be_classified():
    """Contradictory forged inputs still cannot reach range-based classification."""
    forged_true_with_none = RangeCheckedValue(
        test_name="ForgedTrue", loinc_code="", value=1.0, unit="U",
        reference_low=None, reference_high=None, range_available=True,
    )
    forged_zero_range = RangeCheckedValue(
        test_name="ForgedZero", loinc_code="", value=1.0, unit="U",
        reference_low=0.0, reference_high=0.0, in_range=False, range_available=False,
    )
    results, llm_mock = _run_classify([forged_true_with_none, forged_zero_range])
    llm_mock.assert_not_called()
    by_name = {r.test_name: r for r in results}
    assert by_name["ForgedTrue"].status == "unavailable"   # True + None refs -> unavailable
    assert by_name["ForgedZero"].status == "unavailable"   # False + 0-0 -> unavailable, never critical
    print("  PASS: malformed range states (True+None, False+0-0) stay unavailable")


# ============================================================
# Identity matching — duplicate test names / LOINC validation
# ============================================================

def test_duplicate_test_name_unavailable_never_classified():
    """Duplicate test_name: the unavailable twin must never inherit an LLM status.

    test_name alone does not uniquely identify a lab value, so a classification
    is only accepted on exact (test_name, loinc_code, value, unit) identity.
    """
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Glucose", loinc_code="99999-9", value=5.1, unit="mmol/L"),
    ]
    checked = lookup_ranges(values)
    assert checked[0].test_name == checked[1].test_name == "Glucose"
    assert checked[0].range_available is True
    assert checked[1].range_available is False, "unknown LOINC must be unavailable"
    assert checked[1].reference_low is None and checked[1].reference_high is None

    llm_items = [
        # Valid classification for the available glucose:
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
         "status": "normal", "reasoning": "92 is between 70 and 100"},
        # Rogue classification targeting the unavailable twin (same test_name):
        {"test_name": "Glucose", "loinc_code": "99999-9", "value": 5.1, "unit": "mmol/L",
         "status": "critical", "reasoning": "rogue classification"},
    ]
    results, llm_mock = _run_classify(checked, llm_items=llm_items)
    llm_mock.assert_called_once()

    # Only the available value was sent to the LLM:
    prompt = llm_mock.call_args[0][1]["input"]
    assert prompt.count('"range_available"') == 1
    assert '"value": 5.1' not in prompt, "the unavailable twin must not be sent to the LLM"

    by_loinc = {r.loinc_code: r for r in results}
    assert by_loinc["2345-7"].status == "normal", "valid exact-identity output must be accepted"
    assert by_loinc["99999-9"].status == "unavailable", \
        "same test_name must NOT let the unavailable twin inherit the LLM classification"
    assert by_loinc["99999-9"].status not in ("normal", "mildly_abnormal", "critical")
    print("  PASS: duplicate test_name — unavailable twin never inherits LLM classification")


def test_wrong_loinc_classification_rejected():
    """Correct test_name + value + unit but WRONG loinc_code must be rejected."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL")]
    checked = lookup_ranges(values)
    assert checked[0].range_available is True

    llm_items = [{
        "test_name": "Glucose", "loinc_code": "WRONG-LOINC", "value": 92.0, "unit": "mg/dL",
        "status": "critical", "reasoning": "wrong identity",
    }]
    results, _ = _run_classify(checked, llm_items=llm_items)
    # The mismatched identity must NOT become a RiskFlaggedValue:
    assert results == [], "classification with wrong loinc_code must be rejected"
    print("  PASS: wrong loinc_code classification rejected")


def test_missing_loinc_classification_rejected():
    """Correct test_name + value + unit but MISSING loinc_code must be rejected.

    The LOINC is part of the lab result's identity and the system prompt
    requires the model to echo it exactly — it must never be inferred
    from test_name.
    """
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL")]
    checked = lookup_ranges(values)
    assert checked[0].range_available is True

    llm_items = [{
        "test_name": "Glucose", "value": 92.0, "unit": "mg/dL",
        "status": "normal", "reasoning": "no loinc provided",
    }]
    results, _ = _run_classify(checked, llm_items=llm_items)
    assert results == [], \
        "classification without loinc_code must be rejected, never guessed from test_name"
    print("  PASS: missing loinc_code classification rejected (no test_name inference)")


# ============================================================
# Step 4/10 — unavailable survives the orchestrator (non-LLM E2E)
# ============================================================

def test_unavailable_survives_orchestrator():
    """End-to-end non-LLM: lookup -> orchestrator -> risk flagger guard.

    Proves unavailable stays unavailable across the orchestrator boundary and
    through serialization, with zero LLM calls anywhere in the risk step.
    """
    import pipeline.orchestrator as orchestrator_module

    extracted = [
        ExtractedLabValue(test_name="Mystery", loinc_code="99999-9", value=42.0, unit="U"),
        ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL"),
        ExtractedLabValue(test_name="GlucoseMmol", loinc_code="2345-7", value=5.1, unit="mmol/L"),
    ]

    explain_mock = AsyncMock(return_value=[
        FinalExplanation(
            test_name="Mystery",
            explanation="synthetic non-LLM explanation",
            doctor_questions=["Question for the doctor?"],
            citation="synthetic citation",
        )
    ])
    verify_mock = AsyncMock(return_value=VerifierResult(
        passed=True, issues_found=[], action="approve",
    ))

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            orchestrator_module, "pdf_to_text", return_value="synthetic report text"))
        stack.enter_context(patch.object(
            orchestrator_module, "extract_lab_values", AsyncMock(return_value=extracted)))
        stack.enter_context(patch.object(orchestrator_module, "explain", explain_mock))
        stack.enter_context(patch.object(orchestrator_module, "verify", verify_mock))
        # Any real LLM call in the risk step fails the test:
        stack.enter_context(patch("agents.risk_flagger.call_model", _forbidden_call_model()))
        stack.enter_context(patch(
            "agents.risk_flagger.get_client",
            MagicMock(side_effect=AssertionError("LLM client must not be created")),
        ))
        result = asyncio.run(orchestrator_module.run_pipeline("dummy_report.pdf"))

    assert result["verified"] is True

    # The risk flagger output that flowed through the orchestrator:
    risk_flagged = explain_mock.call_args[0][0]
    assert len(risk_flagged) == 3
    by_name = {r.test_name: r for r in risk_flagged}
    for name, r in by_name.items():
        assert r.status == "unavailable", f"{name}: status became {r.status!r}"
        assert r.status not in ("normal", "mildly_abnormal", "critical")
        assert "no range-based risk classification was performed" in r.reasoning

    # Specific causes (range_note provenance) survive to the pipeline boundary:
    assert "not in the supported labs list" in by_name["Mystery"].reasoning
    assert "Sex-specific ranges available" in by_name["Hemoglobin"].reasoning
    assert "Unit mismatch" in by_name["GlucoseMmol"].reasoning

    # The verifier received the same statuses:
    verified_risk = verify_mock.call_args[0][1]
    assert all(r.status == "unavailable" for r in verified_risk)

    # Serialization of the orchestrator's risk payload keeps 'unavailable':
    payload = json.loads(json.dumps([r.model_dump() for r in risk_flagged]))
    assert [item["status"] for item in payload] == ["unavailable"] * 3
    restored = [RiskFlaggedValue.model_validate(item) for item in payload]
    assert all(r.status == "unavailable" for r in restored)

    print("  PASS: unavailable survives lookup -> orchestrator -> risk flagger (zero LLM calls)")


# ============================================================
# Step 7 — reference provenance survives the pipeline
# ============================================================

def test_reference_provenance_survives_pipeline():
    """loinc_code (citation anchor) and range_note survive lookup -> risk flagging."""
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL"),
        ExtractedLabValue(test_name="Mystery", loinc_code="99999-9", value=1.0, unit="U"),
    ]
    checked = lookup_ranges(values)

    # range_note (lookup reasoning) survives lookup -> RangeCheckedValue conversion:
    notes = {c.test_name: c.range_note for c in checked}
    assert notes["Glucose"] == ""
    assert "Sex-specific ranges available" in notes["Hemoglobin"]
    assert "not in the supported labs list" in notes["Mystery"]

    llm_items = [{
        "test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
        "status": "normal", "reasoning": "between 70 and 100",
    }]
    results, _ = _run_classify(checked, llm_items=llm_items)

    # loinc_code survives for every value — available and unavailable alike —
    # which is what the explainer uses for MedlinePlus citation lookup:
    loincs = {r.test_name: r.loinc_code for r in results}
    assert loincs["Glucose"] == "2345-7"
    assert loincs["Hemoglobin"] == "718-7"
    assert loincs["Mystery"] == "99999-9"

    by_name = {r.test_name: r for r in results}
    assert by_name["Glucose"].status == "normal"
    assert "Sex-specific ranges available" in by_name["Hemoglobin"].reasoning
    print("  PASS: provenance (loinc_code + range_note) survives the pipeline")


# ============================================================
# Steps 8.9–8.12 — valid paths and Task 1 behavior unchanged
# ============================================================

def test_valid_reference_range_still_classified_normally():
    """A valid reference range still flows to the LLM and classifies normally."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL")]
    checked = lookup_ranges(values)
    c = checked[0]
    assert c.range_available is True
    assert c.reference_low == 70.0
    assert c.reference_high == 100.0
    assert c.in_range is True

    llm_items = [{
        "test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL",
        "status": "normal", "reasoning": "92 is between 70 and 100",
    }]
    results, llm_mock = _run_classify(checked, llm_items=llm_items)
    llm_mock.assert_called_once()
    prompt = llm_mock.call_args[0][1]["input"]
    assert '"reference_low": 70.0' in prompt
    assert '"reference_high": 100.0' in prompt
    r = results[0]
    assert r.status == "normal"
    assert r.loinc_code == "2345-7", "exact-identity LLM output (correct loinc_code) must be accepted"
    print("  PASS: valid reference range still classified normally")


def test_valid_unit_conversion_still_classified():
    """Supported numerical conversion (K/uL -> cells/mcL) still works end-to-end."""
    values = [ExtractedLabValue(test_name="WBC count", loinc_code="6690-2", value=7.0, unit="K/uL")]
    checked = lookup_ranges(values)
    c = checked[0]
    assert c.value == 7000.0
    assert c.unit == "cells/mcL"
    assert c.range_available is True
    assert c.in_range is True
    assert c.reference_low == 4500.0
    assert c.reference_high == 11000.0

    llm_items = [{
        "test_name": "WBC count", "loinc_code": "6690-2", "value": 7000.0, "unit": "cells/mcL",
        "status": "normal", "reasoning": "7000 is between 4500 and 11000",
    }]
    results, _ = _run_classify(checked, llm_items=llm_items)
    assert results[0].status == "normal"
    assert results[0].loinc_code == "6690-2"
    print("  PASS: valid unit conversion (K/uL -> cells/mcL) still classified normally")


def test_boundary_values_remain_correct():
    """Inclusive boundary behavior (Task 1) is unchanged."""
    values = [
        ExtractedLabValue(test_name="Glucose69", loinc_code="2345-7", value=69.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Glucose70", loinc_code="2345-7", value=70.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Glucose100", loinc_code="2345-7", value=100.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Glucose101", loinc_code="2345-7", value=101.0, unit="mg/dL"),
    ]
    checked = lookup_ranges(values)
    assert [c.in_range for c in checked] == [False, True, True, False]
    assert all(c.range_available for c in checked)
    assert checked[1].reference_low == 70.0
    assert checked[2].reference_high == 100.0
    print("  PASS: boundary values remain correctly classified (inclusive)")


def test_task1_schema_contract_intact():
    """Task 1 schema defaults and status Literal are unchanged."""
    v = RangeCheckedValue(test_name="Test", loinc_code="1-1", value=1.0, unit="U")
    assert v.reference_low is None
    assert v.reference_high is None
    assert v.range_available is False
    assert v.in_range is False

    for status in ("normal", "mildly_abnormal", "critical", "unavailable"):
        f = RiskFlaggedValue(test_name="Test", value=1.0, unit="U", status=status, reasoning="r")
        assert f.status == status
    with pytest.raises(ValidationError):
        RiskFlaggedValue(test_name="Test", value=1.0, unit="U", status="severe", reasoning="r")
    print("  PASS: Task 1 schema contract intact")


if __name__ == "__main__":
    print("=== P0-T2 Reference-Range Safety Consumer Audit Tests ===\n")
    test_case_a_unknown_loinc_is_unavailable()
    test_case_b_missing_sex_context_is_unavailable()
    test_case_c_unit_mismatch_is_unavailable_and_skips_llm()
    test_none_reference_values_never_become_zero()
    test_range_available_false_survives_serialization()
    test_unavailable_value_not_sent_to_range_based_llm()
    test_rogue_llm_status_for_unavailable_value_is_dropped()
    test_malformed_range_states_cannot_be_classified()
    test_duplicate_test_name_unavailable_never_classified()
    test_wrong_loinc_classification_rejected()
    test_missing_loinc_classification_rejected()
    test_unavailable_survives_orchestrator()
    test_reference_provenance_survives_pipeline()
    test_valid_reference_range_still_classified_normally()
    test_valid_unit_conversion_still_classified()
    test_boundary_values_remain_correct()
    test_task1_schema_contract_intact()
    print("\nAll P0-T2 safety tests passed.")
