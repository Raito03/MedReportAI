"""Pydantic models — Section 5 of PROJECT.md. All agent I/O conforms to these."""

from pydantic import BaseModel
from typing import List


class ExtractedLabValue(BaseModel):
    """Output of Extraction Agent."""
    test_name: str
    loinc_code: str
    value: float
    unit: str


class RangeCheckedValue(BaseModel):
    """Output of Reference Range Lookup tool."""
    test_name: str
    loinc_code: str
    value: float
    unit: str
    reference_low: float
    reference_high: float
    in_range: bool


class RiskFlaggedValue(BaseModel):
    """Output of Risk Flagging Agent."""
    test_name: str
    loinc_code: str = ""  # Threaded from extraction for citation lookup
    value: float
    unit: str
    status: str  # "normal" | "mildly_abnormal" | "critical"
    reasoning: str


class FinalExplanation(BaseModel):
    """Output of Explanation Agent, before verification."""
    test_name: str
    explanation: str
    doctor_questions: List[str]
    citation: str


class VerifierResult(BaseModel):
    """Output of Verifier Agent."""
    passed: bool
    issues_found: List[str]
    action: str  # "approve" | "send_back_for_correction"
