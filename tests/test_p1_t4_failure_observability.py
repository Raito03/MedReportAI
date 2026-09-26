"""P1-T4 — Failure Handling + Observability Tests (Hardened).

Proves that the REAL MedReportAI pipeline produces correct observability
events during both success and failure scenarios. Uses FakeLLM and mocked
HTTP wherever possible — no internet access required for deterministic tests.

Sections:
  A. Observability infrastructure unit tests
  B. Real pipeline success observability
  C. Real pipeline failure observability (PDF, LLM, reference, MedlinePlus, verifier)
  D. Failure propagation observability
  E. Retry observability
  F. Privacy
  G. Latency
  H. Serialization
  I. Taxonomy consistency
  J. P0 regression
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import (
    ExtractedLabValue, RangeCheckedValue, RiskFlaggedValue,
    FinalExplanation, VerifierResult
)
from core.observability import (
    FailureType, PipelineStage, PipelineEvent, PipelineLogger,
    LatencyTracker, classify_exception,
    contains_sensitive_data, sanitize_metadata,
    CORE_FAILURE_TYPE_COUNT, TOTAL_FAILURE_TYPE_COUNT,
)
from tests.fake_llm import FakeLLM, use_fake_llm
from tools.pdf_extractor import PdfExtractionError, PdfInvalidError, PdfNoTextError
from pipeline.orchestrator import run_pipeline, MAX_RETRIES

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _build_test_pdf(path: Path, text: str = "Glucose 145 mg/dL (ref 70-100)\nCreatinine 0.9 mg/dL (ref 0.6-1.3)") -> Path:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 700
    for line in text.split("\n"):
        c.drawString(72, y, line)
        y -= 20
    c.showPage()
    c.save()
    return path


_TRUSTED_PAGES = {
    "2345-7": ("Glucose Guide", "https://medlineplus.gov/glucose"),
    "2160-0": ("Creatinine Guide", "https://medlineplus.gov/creatinine"),
}


def _canned_urlopen(request, timeout=None, context=None):
    url = getattr(request, "full_url", str(request))
    entry = None
    for loinc, (title, page_url) in _TRUSTED_PAGES.items():
        if loinc in url:
            entry = {"title": {"_value": title}, "link": [{"href": page_url}], "summary": {"_value": "Test entry."}}
            break
    payload = {"feed": {"entry": [entry] if entry else []}}
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = lambda self: self
    response.__exit__ = MagicMock(return_value=False)
    return response


EXTRACT_RESPONSE = [
    {"test_name": "Glucose", "loinc_code": "2345-7", "value": 145.0, "unit": "mg/dL"},
    {"test_name": "Creatinine", "loinc_code": "2160-0", "value": 0.9, "unit": "mg/dL"},
]

RISK_RESPONSE = [
    {"test_name": "Glucose", "loinc_code": "2345-7", "value": 145.0, "unit": "mg/dL",
     "status": "critical", "reasoning": "Above range"},
    {"test_name": "Creatinine", "loinc_code": "2160-0", "value": 0.9, "unit": "mg/dL",
     "status": "normal", "reasoning": "Within range"},
]

EXPLAIN_RESPONSE = [
    {"test_name": "Glucose", "explanation": "Glucose measures blood sugar.", "doctor_questions": ["What next?"],
     "citation": "MedlinePlus: Glucose (https://medlineplus.gov/glucose)"},
    {"test_name": "Creatinine", "explanation": "Creatinine measures kidney function.", "doctor_questions": ["What next?"],
     "citation": "MedlinePlus: Creatinine (https://medlineplus.gov/creatinine)"},
]

VERIFIER_PASS = json.dumps({"passed": True, "issues_found": []})
VERIFIER_FAIL = json.dumps({"passed": False, "issues_found": ["Missing citation"]})


def _run_with_obs(pdf_path, fake_llm, urlopen_side_effect=None):
    """Run the real pipeline with observability, return (result, logger)."""
    logger = PipelineLogger()
    if urlopen_side_effect is None:
        urlopen_side_effect = _canned_urlopen
    with use_fake_llm(fake_llm), \
         patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=urlopen_side_effect):
        result = asyncio.run(run_pipeline(str(pdf_path), logger=logger))
    return result, logger


# ===========================================================================
# A. Observability infrastructure unit tests
# ===========================================================================

class TestObservabilityInfra:
    def test_failure_type_enum_count(self):
        assert len(FailureType) == TOTAL_FAILURE_TYPE_COUNT

    def test_pipeline_event_to_dict(self):
        e = PipelineEvent(stage=PipelineStage.EXTRACTION, status="success", latency_ms=12.5)
        d = e.to_dict()
        assert d["stage"] == "extraction"
        assert d["status"] == "success"
        assert d["latency_ms"] == 12.5
        assert "failure_type" not in d

    def test_pipeline_event_failure_to_dict(self):
        e = PipelineEvent(stage=PipelineStage.EXTRACTION, status="failed",
                          failure_type=FailureType.LLM_TIMEOUT, reason="timed out",
                          retry_count=2, latency_ms=5000.0)
        d = e.to_dict()
        assert d["failure_type"] == "llm_timeout"
        assert d["retry_count"] == 2
        assert d["reason"] == "timed out"

    def test_classify_exception_pdf(self):
        assert classify_exception(PdfInvalidError("bad")) == FailureType.PDF_ERROR

    def test_classify_exception_timeout(self):
        assert classify_exception(TimeoutError("timeout")) == FailureType.LLM_TIMEOUT

    def test_classify_exception_connection(self):
        assert classify_exception(ConnectionError("refused")) == FailureType.LLM_NETWORK_ERROR

    def test_classify_exception_json(self):
        assert classify_exception(json.JSONDecodeError("err", "", 0)) == FailureType.LLM_PARSE_ERROR

    def test_classify_exception_validation(self):
        assert classify_exception(ValidationError.from_exception_data("test", [])) == FailureType.SCHEMA_VALIDATION_ERROR

    def test_classify_exception_unknown(self):
        assert classify_exception(RuntimeError("something")) == FailureType.UNKNOWN_FAILURE


# ===========================================================================
# B. Real pipeline success observability
# ===========================================================================

class TestRealPipelineSuccess:
    def test_success_emits_all_stage_events(self, tmp_path):
        """Real pipeline emits success events for every stage."""
        pdf = _build_test_pdf(tmp_path / "ok.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        result, logger = _run_with_obs(pdf, fake)

        assert result["verified"] is True
        stages_emitted = {e.stage for e in logger.events}
        for expected_stage in [PipelineStage.PDF_EXTRACTION, PipelineStage.EXTRACTION,
                               PipelineStage.REFERENCE_LOOKUP, PipelineStage.RISK_FLAGGING,
                               PipelineStage.EXPLANATION, PipelineStage.VERIFICATION]:
            assert expected_stage in stages_emitted, f"Missing stage: {expected_stage}"

    def test_success_events_have_latency(self, tmp_path):
        """Every success event has latency_ms >= 0."""
        pdf = _build_test_pdf(tmp_path / "ok.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        for event in logger.events:
            assert event.latency_ms is not None, f"Missing latency for {event.stage}"
            assert event.latency_ms >= 0, f"Negative latency for {event.stage}"

    def test_success_events_have_status(self, tmp_path):
        """All events in success path have status='success'."""
        pdf = _build_test_pdf(tmp_path / "ok.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        for event in logger.events:
            assert event.status == "success", f"Wrong status for {event.stage}: {event.status}"

    def test_success_metadata_has_counts(self, tmp_path):
        """Success events include useful metadata."""
        pdf = _build_test_pdf(tmp_path / "ok.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        extract_events = logger.get_events_for_stage(PipelineStage.EXTRACTION)
        assert len(extract_events) == 1
        assert extract_events[0].metadata.get("lab_value_count") == 2

        risk_events = logger.get_events_for_stage(PipelineStage.RISK_FLAGGING)
        assert len(risk_events) == 1
        assert "status_counts" in risk_events[0].metadata

    def test_success_no_sensitive_data_in_events(self, tmp_path):
        """No raw PDF text or patient data appears in event metadata."""
        pdf = _build_test_pdf(tmp_path / "ok.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        serialized = json.dumps([e.to_dict() for e in logger.events])
        assert "Glucose 145" not in serialized
        assert "John Doe" not in serialized
        assert "P0-T7 Synthetic" not in serialized


# ===========================================================================
# C. Real pipeline failure observability
# ===========================================================================

class TestRealPipelinePDFFailure:
    def test_pdf_failure_emits_event(self, tmp_path):
        """PDF extraction failure emits a failed event."""
        corrupt = tmp_path / "corrupt.pdf"
        corrupt.write_bytes(b"not a pdf")
        logger = PipelineLogger()
        with use_fake_llm(FakeLLM(responses=[])):
            with pytest.raises(PdfInvalidError):
                asyncio.run(run_pipeline(str(corrupt), logger=logger))

        events = logger.get_events_for_stage(PipelineStage.PDF_EXTRACTION)
        assert len(events) == 1
        assert events[0].status == "failed"
        assert events[0].failure_type == FailureType.PDF_ERROR

    def test_pdf_failure_no_downstream_stages(self, tmp_path):
        """PDF failure produces only the PDF stage event — no downstream."""
        corrupt = tmp_path / "corrupt.pdf"
        corrupt.write_bytes(b"not a pdf")
        logger = PipelineLogger()
        with use_fake_llm(FakeLLM(responses=[])):
            with pytest.raises(PdfInvalidError):
                asyncio.run(run_pipeline(str(corrupt), logger=logger))

        stages = {e.stage for e in logger.events}
        assert stages == {PipelineStage.PDF_EXTRACTION}

    def test_pdf_failure_no_llm_calls(self, tmp_path):
        """PDF failure results in zero LLM calls."""
        corrupt = tmp_path / "corrupt.pdf"
        corrupt.write_bytes(b"not a pdf")
        fake = FakeLLM(responses=[])
        logger = PipelineLogger()
        with use_fake_llm(fake):
            with pytest.raises(PdfInvalidError):
                asyncio.run(run_pipeline(str(corrupt), logger=logger))
        assert fake.call_count == 0


class TestRealPipelineLLMFailure:
    def test_extraction_timeout_emits_event(self, tmp_path):
        """Extraction LLM timeout emits correct failure event."""
        pdf = _build_test_pdf(tmp_path / "timeout.pdf")
        fake = FakeLLM(exception=TimeoutError("connection timed out"))
        logger = PipelineLogger()
        with use_fake_llm(fake), \
             patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_canned_urlopen):
            with pytest.raises(TimeoutError):
                asyncio.run(run_pipeline(str(pdf), logger=logger))

        events = logger.get_events_for_stage(PipelineStage.EXTRACTION)
        assert len(events) == 1
        assert events[0].status == "failed"
        assert events[0].failure_type == FailureType.LLM_TIMEOUT

    def test_extraction_failure_no_downstream(self, tmp_path):
        """Extraction failure produces no downstream stage events."""
        pdf = _build_test_pdf(tmp_path / "fail.pdf")
        fake = FakeLLM(exception=ConnectionError("network error"))
        logger = PipelineLogger()
        with use_fake_llm(fake), \
             patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_canned_urlopen):
            with pytest.raises(ConnectionError):
                asyncio.run(run_pipeline(str(pdf), logger=logger))

        stages = {e.stage for e in logger.events}
        assert PipelineStage.PDF_EXTRACTION in stages
        assert PipelineStage.EXTRACTION in stages
        assert PipelineStage.REFERENCE_LOOKUP not in stages
        assert PipelineStage.RISK_FLAGGING not in stages

    def test_risk_failure_emits_event(self, tmp_path):
        """Risk classification failure emits correct event."""
        pdf = _build_test_pdf(tmp_path / "risk_fail.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, TimeoutError("risk timeout")])
        logger = PipelineLogger()
        with use_fake_llm(fake), \
             patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_canned_urlopen):
            with pytest.raises(TimeoutError):
                asyncio.run(run_pipeline(str(pdf), logger=logger))

        risk_events = logger.get_events_for_stage(PipelineStage.RISK_FLAGGING)
        assert len(risk_events) == 1
        assert risk_events[0].status == "failed"
        assert risk_events[0].failure_type == FailureType.LLM_TIMEOUT

    def test_explanation_failure_emits_event(self, tmp_path):
        """Explanation failure emits correct event."""
        pdf = _build_test_pdf(tmp_path / "explain_fail.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, TimeoutError("explain timeout")])
        logger = PipelineLogger()
        with use_fake_llm(fake), \
             patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_canned_urlopen):
            with pytest.raises(TimeoutError):
                asyncio.run(run_pipeline(str(pdf), logger=logger))

        explain_events = logger.get_events_for_stage(PipelineStage.EXPLANATION)
        assert len(explain_events) == 1
        assert explain_events[0].status == "failed"


class TestRealPipelineMedlinePlusFailure:
    def test_medlineplus_timeout_preserves_citation_safety(self, tmp_path):
        """MedlinePlus timeout produces fallback citation, no fabrication."""
        pdf = _build_test_pdf(tmp_path / "mp_timeout.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])

        def _timeout_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("connection timed out")

        result, logger = _run_with_obs(pdf, fake, urlopen_side_effect=_timeout_urlopen)
        # Pipeline should still complete with fallback citations
        assert result["verified"] is True
        for exp in result["explanations"]:
            # No fabricated MedlinePlus title should appear
            assert "Glucose Guide" not in exp.get("citation", "") or "medlineplus.gov" in exp.get("citation", "")

    def test_medlineplus_network_error_preserves_safety(self, tmp_path):
        """MedlinePlus network error produces controlled fallback."""
        pdf = _build_test_pdf(tmp_path / "mp_network.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])

        def _network_error_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("network unreachable")

        result, _ = _run_with_obs(pdf, fake, urlopen_side_effect=_network_error_urlopen)
        assert result["verified"] is True
        # Citations should use fallback pages, not fabricated content
        for exp in result["explanations"]:
            citation = exp.get("citation", "")
            assert "no MedlinePlus citation available" in citation or "medlineplus.gov" in citation


# ===========================================================================
# D. Failure propagation observability
# ===========================================================================

class TestFailurePropagation:
    def test_upstream_failure_stops_pipeline(self, tmp_path):
        """Failure in an early stage prevents all downstream stages."""
        pdf = _build_test_pdf(tmp_path / "prop.pdf")
        fake = FakeLLM(exception=RuntimeError("extraction exploded"))
        logger = PipelineLogger()
        with use_fake_llm(fake), \
             patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_canned_urlopen):
            with pytest.raises(RuntimeError):
                asyncio.run(run_pipeline(str(pdf), logger=logger))

        stages = {e.stage for e in logger.events}
        # Only PDF + extraction stages should appear
        assert PipelineStage.REFERENCE_LOOKUP not in stages
        assert PipelineStage.RISK_FLAGGING not in stages
        assert PipelineStage.EXPLANATION not in stages
        assert PipelineStage.VERIFICATION not in stages


# ===========================================================================
# E. Retry observability
# ===========================================================================

class TestRetryObservability:
    def test_verifier_rejection_retry_count_observable(self, tmp_path):
        """Verifier rejection with retry records correct retry_count."""
        pdf = _build_test_pdf(tmp_path / "retry.pdf")
        # Pipeline calls: extract, risk, explain, verify(fail), explain(retry), verify(pass)
        fake = FakeLLM(responses=[
            EXTRACT_RESPONSE,   # 1. extraction
            RISK_RESPONSE,      # 2. risk classification
            EXPLAIN_RESPONSE,   # 3. explanation (attempt 1)
            VERIFIER_FAIL,      # 4. verification (attempt 1) — fails
            EXPLAIN_RESPONSE,   # 5. explanation (attempt 2, retry)
            VERIFIER_PASS,      # 6. verification (attempt 2) — passes
        ])
        _, logger = _run_with_obs(pdf, fake)

        verify_events = logger.get_events_for_stage(PipelineStage.VERIFICATION)
        assert len(verify_events) == 2
        # First attempt: retry_count=0, second attempt: retry_count=1
        assert verify_events[0].retry_count == 0
        assert verify_events[1].retry_count == 1

    def test_verifier_rejection_terminated(self, tmp_path):
        """Verifier rejection after MAX_RETRIES terminates without infinite loop."""
        pdf = _build_test_pdf(tmp_path / "term.pdf")
        # Pipeline: extract, risk, explain, verify(fail), explain(retry), verify(fail)
        # Total calls: 4 (first cycle) + 2 (retry cycle) = 6
        fake = FakeLLM(responses=[
            EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_FAIL,
            EXPLAIN_RESPONSE, VERIFIER_FAIL,
        ])
        result, logger = _run_with_obs(pdf, fake)

        assert result["verified"] is False
        verify_events = logger.get_events_for_stage(PipelineStage.VERIFICATION)
        assert len(verify_events) == MAX_RETRIES + 1
        # Last event should have the highest retry_count
        assert verify_events[-1].retry_count == MAX_RETRIES


# ===========================================================================
# F. Privacy
# ===========================================================================

class TestPrivacy:
    def test_sensitive_data_detection(self):
        assert contains_sensitive_data("SSN: 123-45-6789") is True
        assert contains_sensitive_data("Phone: 5551234567") is True
        assert contains_sensitive_data("Email: test@example.com") is True
        assert contains_sensitive_data("Normal lab result") is False
        assert contains_sensitive_data("") is False

    def test_nested_metadata_sanitization(self):
        """Nested dicts and lists are recursively sanitized."""
        metadata = {
            "patient": {"dob": "01/02/1990", "name": "John Smith"},
            "results": [{"ssn": "123-45-6789"}, {"safe": "value"}],
            "top_level_ssn": "123-45-6789",
        }
        sanitized = sanitize_metadata(metadata)
        # SSN patterns are caught at any depth
        assert sanitized["results"][0]["ssn"] == "[REDACTED]"
        assert sanitized["top_level_ssn"] == "[REDACTED]"
        # DOB in MM/DD/YYYY format is caught
        assert sanitized["patient"]["dob"] == "[REDACTED]"
        # Non-sensitive nested data passes through
        assert sanitized["results"][1]["safe"] == "value"
        # Note: arbitrary names like "John Smith" are NOT caught by pattern
        # matching — that would require NLP/NER which is out of scope.
        # The privacy contract is pattern-based PHI detection, not full NER.

    def test_long_string_truncation(self):
        result = sanitize_metadata({"data": "x" * 2000})
        assert len(result["data"]) < 200
        assert "TRUNCATED" in result["data"]

    def test_event_serialization_no_phi(self, tmp_path):
        """Serialized events from real pipeline contain no patient data."""
        pdf = _build_test_pdf(tmp_path / "phi.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        serialized = json.dumps([e.to_dict() for e in logger.events])
        # No raw PDF content should appear
        assert "Glucose 145 mg/dL" not in serialized
        assert "Creatinine 0.9 mg/dL" not in serialized


# ===========================================================================
# G. Latency
# ===========================================================================

class TestLatency:
    def test_latency_tracker_basic(self):
        tracker = LatencyTracker()
        tracker.start("test")
        time.sleep(0.05)
        latency = tracker.end("test")
        assert latency > 0
        stats = tracker.get_stats("test")
        assert stats["count"] == 1
        assert stats["min"] > 0

    def test_latency_in_real_pipeline(self, tmp_path):
        """Real pipeline events include latency_ms >= 0."""
        pdf = _build_test_pdf(tmp_path / "lat.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        for event in logger.events:
            assert event.latency_ms is not None
            assert event.latency_ms >= 0


# ===========================================================================
# H. Serialization
# ===========================================================================

class TestSerialization:
    def test_event_dict_is_json_serializable(self):
        event = PipelineEvent(
            stage=PipelineStage.RISK_FLAGGING, status="failed",
            failure_type=FailureType.LLM_TIMEOUT, retry_count=1,
            latency_ms=1234.56, reason="timeout after 10s",
            metadata={"count": 5}
        )
        d = event.to_dict()
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["stage"] == "risk_flagging"
        assert parsed["failure_type"] == "llm_timeout"
        assert parsed["latency_ms"] == 1234.56

    def test_all_events_serializable(self, tmp_path):
        """All events from a real pipeline run are JSON-serializable."""
        pdf = _build_test_pdf(tmp_path / "ser.pdf")
        fake = FakeLLM(responses=[EXTRACT_RESPONSE, RISK_RESPONSE, EXPLAIN_RESPONSE, VERIFIER_PASS])
        _, logger = _run_with_obs(pdf, fake)

        serialized = json.dumps([e.to_dict() for e in logger.events])
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert len(parsed) == len(logger.events)


# ===========================================================================
# I. Taxonomy consistency
# ===========================================================================

class TestTaxonomyConsistency:
    def test_failure_type_count_matches_docs(self):
        """Number of FailureType members matches documented count."""
        assert len(FailureType) == TOTAL_FAILURE_TYPE_COUNT
        # Verify the core count is correct
        core_types = [ft for ft in FailureType if ft.value not in
                      ("extraction_failure", "risk_classification_failure", "explanation_failure")]
        assert len(core_types) == CORE_FAILURE_TYPE_COUNT

    def test_all_documented_types_exist(self):
        """Every type mentioned in docs exists in the enum."""
        documented_core = [
            "pdf_error", "llm_timeout", "llm_network_error", "llm_auth_error",
            "llm_rate_limit", "llm_server_error", "llm_parse_error",
            "schema_validation_error", "reference_unavailable",
            "medlineplus_no_match", "medlineplus_timeout", "medlineplus_network_error",
            "medlineplus_invalid_response", "verification_failure", "unknown_failure",
        ]
        for name in documented_core:
            assert FailureType(name) is not None


# ===========================================================================
# J. P0 regression — key safety contracts
# ===========================================================================

class TestP0Regression:
    def test_unknown_loinc_unavailable(self):
        from agents.reference_range import lookup_ranges
        result = lookup_ranges([ExtractedLabValue(test_name="X", loinc_code="99999-9", value=1.0, unit="U")])
        assert result[0].range_available is False
        assert result[0].reference_low is None
        assert result[0].reference_high is None

    def test_sex_specific_unavailable(self):
        from agents.reference_range import lookup_ranges
        result = lookup_ranges([ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL")])
        assert result[0].range_available is False

    def test_unavailable_never_classified(self):
        from agents.risk_flagger import classify_risk
        unavail = [RangeCheckedValue(
            test_name="X", loinc_code="99999-9", value=1.0, unit="U",
            reference_low=None, reference_high=None, in_range=False,
            range_available=False, range_note="test"
        )]
        fake = FakeLLM(responses=[])
        with use_fake_llm(fake):
            result = asyncio.run(classify_risk(unavail))
            assert result[0].status == "unavailable"
            assert fake.call_count == 0

    def test_explainer_empty_input_no_llm(self):
        from agents.explainer import explain
        fake = FakeLLM(responses=[])
        with use_fake_llm(fake):
            result = asyncio.run(explain([]))
            assert result == []
            assert fake.call_count == 0

    def test_verifier_malformed_fails_closed(self):
        from agents.verifier import verify
        explanations = [FinalExplanation(
            test_name="Glucose", explanation="Test.", doctor_questions=["?"],
            citation="MedlinePlus: Test (https://medlineplus.gov/test)",
            citation_url="https://medlineplus.gov/test", citation_status="available",
        )]
        risk = [RiskFlaggedValue(
            test_name="Glucose", loinc_code="2345-7", value=95.0, unit="mg/dL",
            status="normal", reasoning="Within range",
        )]
        fake = FakeLLM(responses=[{"passed": "maybe", "issues_found": "bad"}])
        with use_fake_llm(fake):
            result = asyncio.run(verify(explanations, risk))
            assert result.passed is False
