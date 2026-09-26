"""Schema validation tests — all Pydantic models accept correct data, reject bad."""

import pytest
from pydantic import ValidationError
from core.schemas import (
    ExtractedLabValue,
    RangeCheckedValue,
    RiskFlaggedValue,
    FinalExplanation,
    VerifierResult,
)


def test_extracted_lab_value():
    v = ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=95.0, unit="mg/dL")
    assert v.test_name == "Glucose"
    assert v.value == 95.0


def test_range_checked_value():
    v = RangeCheckedValue(
        test_name="Glucose", loinc_code="2345-7", value=95.0, unit="mg/dL",
        reference_low=70.0, reference_high=100.0, in_range=True,
    )
    assert v.in_range is True


def test_risk_flagged_value():
    v = RiskFlaggedValue(
        test_name="Glucose", value=95.0, unit="mg/dL",
        status="normal", reasoning="Within reference range",
    )
    assert v.status == "normal"


def test_risk_flagged_value_unavailable():
    """status='unavailable' is valid and means no range-based classification was performed."""
    v = RiskFlaggedValue(
        test_name="Hemoglobin", value=14.0, unit="g/dL",
        status="unavailable",
        reasoning="Cannot classify: Sex-specific ranges available but patient sex not provided",
    )
    assert v.status == "unavailable"


def test_risk_flagged_value_all_statuses():
    """All four valid statuses are accepted."""
    for status in ("normal", "mildly_abnormal", "critical", "unavailable"):
        v = RiskFlaggedValue(
            test_name="Test", value=1.0, unit="U",
            status=status, reasoning="test",
        )
        assert v.status == status


def test_risk_flagged_value_rejects_invalid_status():
    """Invalid status values are rejected by the Literal constraint."""
    with pytest.raises(ValidationError):
        RiskFlaggedValue(
            test_name="Test", value=1.0, unit="U",
            status="severe", reasoning="test",
        )


def test_final_explanation():
    e = FinalExplanation(
        test_name="Glucose",
        explanation="Glucose measures blood sugar levels.",
        doctor_questions=["What should my target range be?"],
        citation="MedlinePlus: Blood Glucose",
    )
    assert len(e.doctor_questions) == 1


def test_verifier_result_pass():
    r = VerifierResult(passed=True, issues_found=[], action="approve")
    assert r.passed is True


def test_verifier_result_fail():
    r = VerifierResult(
        passed=False,
        issues_found=["Diagnostic language detected"],
        action="send_back_for_correction",
    )
    assert r.action == "send_back_for_correction"


if __name__ == "__main__":
    test_extracted_lab_value()
    test_range_checked_value()
    test_risk_flagged_value()
    test_risk_flagged_value_unavailable()
    test_risk_flagged_value_all_statuses()
    test_risk_flagged_value_rejects_invalid_status()
    test_final_explanation()
    test_verifier_result_pass()
    test_verifier_result_fail()
    print("All schema tests passed.")
