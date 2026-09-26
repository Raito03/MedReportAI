"""Pydantic models — Section 5 of PROJECT.md. All agent I/O conforms to these."""

from pydantic import BaseModel
from typing import List, Literal, Optional


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
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    in_range: bool = False
    range_available: bool = False
    # Why a range is unavailable (empty string when a range was found).
    # Propagated from the lookup tool so downstream reasoning keeps the
    # specific cause (unknown LOINC / unit mismatch / missing context).
    range_note: str = ""


class RiskFlaggedValue(BaseModel):
    """Output of Risk Flagging Agent.

    Status values:
    - "normal": value within reference range
    - "mildly_abnormal": value slightly outside range
    - "critical": value far outside range
    - "unavailable": no valid applicable reference range; no range-based
      classification was performed. This does NOT mean the value is
      abnormal or critical — the system simply cannot assess it.
    """
    test_name: str
    loinc_code: str = ""  # Threaded from extraction for citation lookup
    value: float
    unit: str
    status: Literal["normal", "mildly_abnormal", "critical", "unavailable"]
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
