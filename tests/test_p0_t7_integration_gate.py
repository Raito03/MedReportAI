"""P0-T7 — Phase 0 Integration Gate.

A single deterministic end-to-end gate over the REAL production pipeline
(pipeline.orchestrator.run_pipeline) — every agent runs for real; only the
external boundaries are controlled, using the P0-T5 fake-LLM infrastructure
and a canned MedlinePlus HTTP response at the existing service boundary
(tools.medlineplus_connect.urllib.request.urlopen).

Flow exercised:

    deterministic lab-panel PDF (reportlab, synthetic values)
      -> tools.pdf_extractor.pdf_to_text            (real, P0-T3)
      -> agents.extraction.extract_lab_values       (real, FakeLLM call #1)
      -> agents.reference_range.lookup_ranges       (real, P0-T1/T2 tool call)
      -> agents.risk_flagger.classify_risk          (real, FakeLLM call #2)
      -> agents.explainer.explain                   (real, FakeLLM call #3,
                                                      real get_citation grounding)
      -> agents.verifier.verify                     (real, FakeLLM call #4)
      -> final structured output (validated against core.schemas)

No network, no OpenRouter, no real patient data, no randomness.
"""

import asyncio
import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as app_config
from core.schemas import FinalExplanation, RangeCheckedValue, RiskFlaggedValue
from pipeline.orchestrator import run_pipeline
from tests.fake_llm import FakeLLM, use_fake_llm
from tools.pdf_extractor import PdfInvalidError, PdfNoTextError

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Deterministic fixture: synthetic lab panel PDF (no real patient data).
# Values exercise abnormal (Glucose 145 > 70-100), normal
# (Creatinine 0.9 within 0.6-1.3), and unavailable (Hemoglobin sex-specific
# ranges with no patient sex) paths, with distinct units per test.
# ---------------------------------------------------------------------------

_FIXTURE_ROWS = [
    ("Glucose", "145", "mg/dL", "70-100", "2345-7", 145.0),
    ("Creatinine", "0.9", "mg/dL", "0.6-1.3", "2160-0", 0.9),
    ("Hemoglobin", "14.2", "g/dL", "12.0-17.5", "718-7", 14.2),
]


def _build_gate_pdf(path: Path) -> Path:
    """Smallest deterministic PDF fixture for the gate (synthetic values)."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, "P0-T7 Synthetic Lab Panel")
    c.setFont("Helvetica", 10)
    c.drawString(72, 700, "Fixture values are synthetic - no real patient data")
    y = 660
    for name, val, unit, ref, _loinc, _v in _FIXTURE_ROWS:
        c.drawString(72, y, f"{name} {val} {unit} (ref {ref})")
        y -= 22
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------------------
# Controlled MedlinePlus boundary (real get_citation path, canned HTTP)
# ---------------------------------------------------------------------------

_TRUSTED_PAGES = {
    "2345-7": ("P0T7 Fixture Glucose Guide", "https://medlineplus.gov/p0t7-glucose"),
    "2160-0": ("P0T7 Fixture Creatinine Guide", "https://medlineplus.gov/p0t7-creatinine"),
    "718-7": ("P0T7 Fixture Hemoglobin Guide", "https://medlineplus.gov/p0t7-hemoglobin"),
}
_TRUSTED_CITATIONS = {
    "Glucose": f"MedlinePlus: {_TRUSTED_PAGES['2345-7'][0]} ({_TRUSTED_PAGES['2345-7'][1]})",
    "Creatinine": f"MedlinePlus: {_TRUSTED_PAGES['2160-0'][0]} ({_TRUSTED_PAGES['2160-0'][1]})",
    "Hemoglobin": f"MedlinePlus: {_TRUSTED_PAGES['718-7'][0]} ({_TRUSTED_PAGES['718-7'][1]})",
}


def _canned_urlopen(request, timeout=None, context=None):
    """Canned MedlinePlus Connect response, chosen by the requested LOINC."""
    url = getattr(request, "full_url", str(request))
    entry = None
    for loinc, (title, page_url) in _TRUSTED_PAGES.items():
        if loinc in url:
            entry = {
                "title": {"_value": title},
                "link": [{"href": page_url}],
                "summary": {"_value": "P0-T7 synthetic grounding entry."},
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
    {"test_name": name, "loinc_code": loinc, "value": value, "unit": unit}
    for name, _val, unit, _ref, loinc, value in _FIXTURE_ROWS
]
RISK_ITEMS = [
    {"test_name": "Glucose", "loinc_code": "2345-7", "value": 145.0, "unit": "mg/dL",
     "status": "critical",
     "reasoning": "145 is far above the 70-100 reference range"},
    {"test_name": "Creatinine", "loinc_code": "2160-0", "value": 0.9, "unit": "mg/dL",
     "status": "normal",
     "reasoning": "0.9 is within the 0.6-1.3 reference range"},
    # Hemoglobin intentionally absent: range unavailable -> the P0-T2 guard
    # never sends it for range-based classification.
]
EXPLAIN_ITEMS = [
    {"test_name": "Glucose",
     "explanation": "Glucose measures blood sugar levels; a result of 145 mg/dL is above the typical reference range of 70-100 mg/dL.",
     "doctor_questions": ["What follow-up testing do you recommend?"],
     "citation": "https://evil.example/p0t7-rogue-citation"},
    {"test_name": "Creatinine",
     "explanation": "Creatinine reflects kidney filtration; a result of 0.9 mg/dL is within the typical reference range of 0.6-1.3 mg/dL.",
     "doctor_questions": ["How often should kidney function be checked?"],
     "citation": "Totally Fabricated Medical Source 42"},
    {"test_name": "Hemoglobin",
     "explanation": "Hemoglobin carries oxygen in the blood; this result could not be assessed against a reference range here - discuss it with your doctor.",
     "doctor_questions": ["Can this test be repeated with population context?"],
     "citation": "https://evil.example/p0t7-rogue-citation"},
]
VERIFIER_PASS = json.dumps({"passed": True, "issues_found": []})


def _verifier_fail(issue="missing citation"):
    return json.dumps({"passed": False, "issues_found": [issue]})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload_after(call, marker):
    """Parse the JSON context an agent embedded after `marker` in its prompt."""
    return json.loads(call["input"].split(marker, 1)[1])


def _verifier_payloads(call):
    prompt = call["input"]
    expl = prompt.split("Explanations:\n", 1)[1].split("\n\nRisk classifications:\n")[0]
    risk = prompt.split("Risk classifications:\n", 1)[1]
    return json.loads(expl), json.loads(risk)


def _run_gate(fake, pdf_path, urlopen_side_effect=_canned_urlopen):
    """Run the REAL orchestrator with FakeLLM + controlled MedlinePlus boundary."""
    with use_fake_llm(fake), \
         patch("tools.medlineplus_connect.urllib.request.urlopen",
               side_effect=urlopen_side_effect):
        result = asyncio.run(run_pipeline(str(pdf_path)))
    return result, fake


def _fresh_gate_fake():
    """A FakeLLM programmed with the four deterministic pipeline responses."""
    return FakeLLM(responses=[EXTRACT_ITEMS, RISK_ITEMS, EXPLAIN_ITEMS, VERIFIER_PASS])


# ---------------------------------------------------------------------------
# Main gate: complete happy-path data flow (Sections 3 + 5)
# ---------------------------------------------------------------------------

def test_phase0_integration_gate(tmp_path):
    """Real orchestrator, real agents, controlled external boundaries."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_panel.pdf")
    fake = _fresh_gate_fake()
    result, fake = _run_gate(fake, pdf)

    # --- PDF -> extraction: fixture content extracted and forwarded ---
    for marker in ("P0-T7 Synthetic Lab Panel", "Glucose", "145",
                   "Creatinine", "0.9", "Hemoglobin", "14.2"):
        assert marker in result["raw_text"], f"fixture text missing: {marker}"
        assert marker in fake.calls[0]["input"], f"text not forwarded to extraction LLM: {marker}"

    # --- expected LLM calls: exactly four, configured model, stage prompts ---
    assert fake.call_count == 4, "pipeline must make exactly 4 LLM calls"
    assert set(fake.models) == {app_config.OPENROUTER_MODEL}
    assert "Classify these lab values" in fake.calls[1]["input"]
    assert "Explain these lab results" in fake.calls[2]["input"]
    assert "Risk classifications" in fake.calls[3]["input"]

    # --- structured labs -> reference ranges (risk-stage input payload) ---
    risk_input = _payload_after(fake.calls[1], "Classify these lab values:\n")
    checked = [RangeCheckedValue.model_validate(r) for r in risk_input]
    by_name = {c.test_name: c for c in checked}
    # Hemoglobin (sex-specific, no context) must NOT be sent for range
    # classification — the P0-T2 unavailable guard holds at gate level:
    assert "Hemoglobin" not in by_name
    glucose, creatinine = by_name["Glucose"], by_name["Creatinine"]
    assert (glucose.loinc_code, glucose.value, glucose.unit) == ("2345-7", 145.0, "mg/dL")
    assert glucose.range_available is True
    assert (glucose.reference_low, glucose.reference_high) == (70.0, 100.0)
    assert (creatinine.loinc_code, creatinine.value, creatinine.unit) == ("2160-0", 0.9, "mg/dL")
    assert creatinine.range_available is True
    assert creatinine.reference_low is not None and creatinine.reference_high is not None

    # --- grounding: explain-stage input carries the trusted citations ---
    expl_ctx = _payload_after(fake.calls[2], "Explain these lab results:\n")
    ctx_by_name = {c["test_name"]: c for c in expl_ctx}
    assert set(ctx_by_name) == {"Glucose", "Creatinine", "Hemoglobin"}
    for name, item in ctx_by_name.items():
        assert item["citation"] == _TRUSTED_CITATIONS[name], \
            f"trusted MedlinePlus citation missing for {name}"
        assert item["status"] in ("normal", "mildly_abnormal", "critical", "unavailable")
        assert item["loinc_code"]
    assert ctx_by_name["Glucose"]["status"] == "critical"
    assert ctx_by_name["Creatinine"]["status"] == "normal"
    assert ctx_by_name["Hemoglobin"]["status"] == "unavailable"

    # --- explanation -> verification: both payloads schema-valid ---
    expl_payload, risk_payload = _verifier_payloads(fake.calls[3])
    expl_v = [FinalExplanation.model_validate(x) for x in expl_payload]
    risk_v = [RiskFlaggedValue.model_validate(x) for x in risk_payload]
    assert {e.test_name for e in expl_v} == {"Glucose", "Creatinine", "Hemoglobin"}
    assert {(r.test_name, r.status) for r in risk_v} == {
        ("Glucose", "critical"), ("Creatinine", "normal"), ("Hemoglobin", "unavailable")}

    # --- final output: validated against the actual schema ---
    assert result["verified"] is True
    assert result["issues"] == []
    explanations = [FinalExplanation.model_validate(e) for e in result["explanations"]]
    assert {e.test_name for e in explanations} == {"Glucose", "Creatinine", "Hemoglobin"}
    for e in explanations:
        assert e.explanation and e.doctor_questions and e.citation
    print("  PASS: Phase 0 integration gate (PDF -> ... -> final output)")


# ---------------------------------------------------------------------------
# Grounding / citation safety (Section 8, P0-T6 boundary)
# ---------------------------------------------------------------------------

def test_gate_trusted_citations_cannot_be_overridden_by_llm(tmp_path):
    """The LLM returns rogue citation URLs — final output must use trusted lookup."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_citation.pdf")
    fake = _fresh_gate_fake()
    result, _fake = _run_gate(fake, pdf)

    dumped = json.dumps(result["explanations"])
    assert "evil.example" not in dumped, "rogue LLM citation URL leaked into final output"
    assert "Totally Fabricated Medical Source" not in dumped, \
        "fabricated LLM citation leaked into final output"
    for e in result["explanations"]:
        assert e["citation"] == _TRUSTED_CITATIONS[e["test_name"]], \
            "trusted MedlinePlus lookup must be the source of the final citation"
    print("  PASS: LLM cannot override trusted MedlinePlus citations")


# ---------------------------------------------------------------------------
# Cross-test contamination (Section 6)
# ---------------------------------------------------------------------------

def test_gate_no_cross_test_contamination(tmp_path):
    """Each test keeps its own value/unit/LOINC/status through the pipeline."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_isolation.pdf")
    fake = _fresh_gate_fake()
    result, _fake = _run_gate(fake, pdf)

    expl_ctx = _payload_after(fake.calls[2], "Explain these lab results:\n")
    by_name = {c["test_name"]: c for c in expl_ctx}
    expected_identity = {
        "Glucose": (145.0, "mg/dL", "2345-7", "critical"),
        "Creatinine": (0.9, "mg/dL", "2160-0", "normal"),
        "Hemoglobin": (14.2, "g/dL", "718-7", "unavailable"),
    }
    for name, (value, unit, loinc, status) in expected_identity.items():
        item = by_name[name]
        assert item["value"] == value, f"{name} value crossed with another test"
        assert item["unit"] == unit, f"{name} unit crossed with another test"
        assert item["loinc_code"] == loinc, f"{name} LOINC crossed with another test"
        assert item["status"] == status, f"{name} status crossed with another test"

    # A numeric value must appear only under its own test:
    for value in (145.0, 0.9, 14.2):
        owners = {c["test_name"] for c in expl_ctx if c["value"] == value}
        assert len(owners) == 1, f"value {value} appears under multiple tests: {owners}"

    # Final explanations pair the right trusted citation with the right test:
    for e in result["explanations"]:
        assert e["citation"] == _TRUSTED_CITATIONS[e["test_name"]]
    print("  PASS: no cross-test contamination of values/units/LOINCs/statuses")


# ---------------------------------------------------------------------------
# Determinism (Section 10)
# ---------------------------------------------------------------------------

def test_gate_is_deterministic(tmp_path):
    """Three full gate runs produce identical structured results."""
    runs = []
    for i in range(3):
        pdf = _build_gate_pdf(tmp_path / f"p0_t7_determinism_{i}.pdf")
        fake = _fresh_gate_fake()
        result, _fake = _run_gate(fake, pdf)
        expl_ctx = _payload_after(fake.calls[2], "Explain these lab results:\n")
        runs.append({
            "verified": result["verified"],
            "issues": result["issues"],
            "explanations": [(e["test_name"], e["explanation"], e["citation"])
                             for e in result["explanations"]],
            "risk_context": [(c["test_name"], c["value"], c["unit"], c["loinc_code"],
                              c["status"], c["citation"]) for c in expl_ctx],
            "call_count": fake.call_count,
        })
    assert runs[0] == runs[1] == runs[2], "gate results must be deterministic"
    print("  PASS: three gate runs produce identical results")


# ---------------------------------------------------------------------------
# Failure propagation (Section 7)
# ---------------------------------------------------------------------------

def test_gate_pdf_extraction_failure_propagates(tmp_path):
    """PDF failures surface as controlled P0-T3 errors; no LLM call happens."""
    fake = _fresh_gate_fake()

    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog")  # truncated/corrupt
    with use_fake_llm(fake):
        with pytest.raises(PdfInvalidError):
            asyncio.run(run_pipeline(str(corrupt)))
    assert fake.call_count == 0, "no downstream LLM call may happen after PDF failure"

    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank), pagesize=letter)  # readable, but no text
    c.showPage()
    c.save()
    with use_fake_llm(fake):
        with pytest.raises(PdfNoTextError) as exc_info:
            asyncio.run(run_pipeline(str(blank)))
    assert exc_info.value.reason == "blank"
    assert fake.call_count == 0
    print("  PASS: PDF extraction failures propagate controlled (no fabricated run)")


def test_gate_structured_extraction_failure_propagates(tmp_path):
    """An invalid structured item fails validation; later stages never run."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_bad_extract.pdf")
    bad_items = [{"test_name": "Glucose", "loinc_code": "2345-7", "value": 145.0}]  # no unit
    fake = FakeLLM(responses=[bad_items, RISK_ITEMS, EXPLAIN_ITEMS, VERIFIER_PASS])
    with use_fake_llm(fake), \
         patch("tools.medlineplus_connect.urllib.request.urlopen",
               side_effect=_canned_urlopen):
        with pytest.raises(ValidationError):
            asyncio.run(run_pipeline(str(pdf)))
    assert fake.call_count == 1, "pipeline must stop after invalid structured output"
    print("  PASS: invalid structured extraction stops the pipeline")


def test_gate_medlineplus_network_failure_controlled(tmp_path):
    """Network failure at the grounding boundary -> controlled fallback citation."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_network.pdf")
    fake = _fresh_gate_fake()

    def _offline(request, timeout=None, context=None):
        raise urllib.error.URLError("network down for P0-T7")

    result, _fake = _run_gate(fake, pdf, urlopen_side_effect=_offline)

    # Contract preserved: pipeline completes; citation falls back per the
    # get_citation priority (fallback page URL) and never fabricates a
    # live-style title that could not actually be fetched:
    mapping = json.loads(
        (REPO_ROOT / "data" / "medlineplus_mapping.json").read_text(encoding="utf-8")
    )["mappings"]
    loinc_by_name = {"Glucose": "2345-7", "Creatinine": "2160-0", "Hemoglobin": "718-7"}
    assert result["verified"] is True
    for e in result["explanations"]:
        name = e["test_name"]
        fallback = mapping[loinc_by_name[name]]["known_human_readable_fallback_page"]
        assert e["citation"] == f"MedlinePlus: {name} ({fallback})"
        assert "P0T7 Fixture" not in e["citation"], \
            "unreachable live-style title must not appear after a network failure"
    print("  PASS: MedlinePlus network failure -> controlled fallback citation")


def test_gate_malformed_llm_response_fails_controlled(tmp_path):
    """Malformed risk-stage LLM output surfaces as ValueError; no result dict."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_malformed.pdf")
    fake = FakeLLM(responses=[EXTRACT_ITEMS, "not json at all, no brackets here",
                               EXPLAIN_ITEMS, VERIFIER_PASS])
    with use_fake_llm(fake), \
         patch("tools.medlineplus_connect.urllib.request.urlopen",
               side_effect=_canned_urlopen):
        with pytest.raises(ValueError):
            asyncio.run(run_pipeline(str(pdf)))
    assert fake.call_count == 2, "pipeline must stop at the malformed response"
    print("  PASS: malformed LLM response fails controlled (no fabricated results)")


def test_gate_verification_failure_reported(tmp_path):
    """Verifier rejection is retried, then reported — never silently approved."""
    pdf = _build_gate_pdf(tmp_path / "p0_t7_verify_fail.pdf")
    fail = _verifier_fail("missing citation")
    fake = FakeLLM(responses=[
        EXTRACT_ITEMS, RISK_ITEMS, EXPLAIN_ITEMS, fail,
        EXPLAIN_ITEMS, fail,  # retry attempt
    ])
    result, _fake = _run_gate(fake, pdf)

    assert fake.call_count == 6, "expected extract+risk+2x(explain+verify)"
    assert result["verified"] is False
    assert result["issues"] == ["missing citation"]
    # Output is still schema-valid — failure reported, not fabricated away:
    explanations = [FinalExplanation.model_validate(e) for e in result["explanations"]]
    assert len(explanations) == 3
    print("  PASS: verification failure surfaces in final output")


# ---------------------------------------------------------------------------
# Schema boundaries (Section 9)
# ---------------------------------------------------------------------------

def test_gate_schema_boundaries():
    """Valid output passes; missing fields and invalid statuses are rejected."""
    # Valid final-output element:
    valid = FinalExplanation(
        test_name="Glucose",
        explanation="Glucose measures blood sugar levels.",
        doctor_questions=["What is my target range?"],
        citation="MedlinePlus: Something (https://medlineplus.gov/x)")
    assert FinalExplanation.model_validate(valid.model_dump()).test_name == "Glucose"

    # Missing required field:
    with pytest.raises(ValidationError):
        FinalExplanation.model_validate({"test_name": "Glucose"})

    # Invalid risk status:
    with pytest.raises(ValidationError):
        RiskFlaggedValue.model_validate(
            {"test_name": "Glucose", "loinc_code": "2345-7", "value": 145.0,
             "unit": "mg/dL", "status": "severe", "reasoning": "bad status"})

    # Missing risk fields:
    with pytest.raises(ValidationError):
        RiskFlaggedValue.model_validate(
            {"test_name": "Glucose", "value": 145.0, "unit": "mg/dL"})
    print("  PASS: schema boundaries enforced (valid accepted, invalid rejected)")


if __name__ == "__main__":
    print("=== P0-T7 Phase 0 Integration Gate ===\n")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        # Adapt pytest-style signatures for direct execution:
        test_phase0_integration_gate(base)
        test_gate_trusted_citations_cannot_be_overridden_by_llm(base)
        test_gate_no_cross_test_contamination(base)
        test_gate_is_deterministic(base)
        test_gate_pdf_extraction_failure_propagates(base)
        test_gate_structured_extraction_failure_propagates(base)
        test_gate_medlineplus_network_failure_controlled(base)
        test_gate_malformed_llm_response_fails_controlled(base)
        test_gate_verification_failure_reported(base)
    test_gate_schema_boundaries()
    print("\nAll P0-T7 integration gate tests passed.")
