"""P0-T6 — MedlinePlus grounding hardening tests.

Deterministic: every MedlinePlus HTTP response is mocked at the
`urllib.request.urlopen` layer, and every LLM call is mocked at the agent
`call_model` layer. No internet access and no OpenRouter API key required.

Covers the P0-T6 roadmap subtasks and the required behavior matrix:
  1  Successful URL response (list-of-dicts + feed shapes)
  2  Multiple returned records
  3  Empty response -> citation unavailable, nothing fabricated
  4  Malformed responses handled without crashing
  5  Invalid/untrusted URL never accepted as a trusted citation
  6  Timeout -> controlled failure, no fabricated citation
  7  Network failure -> controlled failure
  8  Unexpected client exception -> controlled failure
  9  Unknown/unsupported LOINC -> no fabricated citation
 10  Citation propagation: LOINC -> lookup -> explanation input AND output
 11  No-citation propagation: lookup failure -> explicit unavailable state
 12  Citation isolation between different lab results
 +  LLM-cannot-invent-citation checks (invented test_name included)
 +  End-to-end mocked pipeline: reference/risk -> lookup -> explain -> verify
"""

import asyncio
import json
import re
import sys
import os
import urllib.parse
from contextlib import ExitStack
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.schemas import ExtractedLabValue, RiskFlaggedValue
from agents.explainer import explain
from agents.reference_range import lookup_ranges
from tools.medlineplus_connect import (
    CITATION_UNAVAILABLE_MARKER,
    derive_citation_fields,
    fetch_medlineplus_info,
    get_citation,
    is_trusted_medlineplus_url,
)

# Distinct, deterministic citation targets for the two lab identities used
# in the isolation tests.
GLUCOSE_URL = "https://medlineplus.gov/lab-tests/blood-glucose-test/"
WBC_URL = "https://medlineplus.gov/lab-tests/white-blood-count-wbc/"
EVIL_URL = "https://evil.example.com/fake-medlineplus"

UNKNOWN_LOINC = "99999-9"      # absent from data/medlineplus_mapping.json
UNKNOWN_NAME = "Mystery"


# ============================================================
# Helpers — HTTP and LLM are always mocked; never any real call
# ============================================================

def _mock_response(payload):
    """urlopen context-manager mock returning `payload` as JSON bytes."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _urlopen_returning(payload):
    return patch("tools.medlineplus_connect.urllib.request.urlopen",
                 return_value=_mock_response(payload))


def _urlopen_raising(exc):
    return patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=exc)


def _urlopen_dispatch(responses):
    """Mock urlopen answering per-LOINC (keyed by mainSearchCriteria.v.c).

    Unknown LOINCs get an empty feed (a legitimate no-match), so citation
    isolation can be observed through the *requested* code alone.
    """
    def _fake(req, timeout=None, context=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        loinc = query.get("mainSearchCriteria.v.c", [""])[0]
        return _mock_response(responses.get(loinc, {"feed": {"entry": []}}))
    return patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=_fake)


def _feed(title, url):
    """Original project (feed) response shape."""
    return {"feed": {"entry": [{"title": {"_value": title}, "link": {"@href": url}}]}}


def _llm_returning_items(items):
    result = MagicMock()
    result.get_text = AsyncMock(return_value=json.dumps(items))
    return MagicMock(return_value=result)


def _llm_returning_text(text):
    result = MagicMock()
    result.get_text = AsyncMock(return_value=text)
    return MagicMock(return_value=result)


def _payload_after(mock_call, marker):
    prompt = mock_call.call_args[0][1]["input"]
    return json.loads(prompt.split(marker, 1)[1])


def _only_trusted_urls(text):
    for url in re.findall(r"https?://[^)\s]+", text):
        if not is_trusted_medlineplus_url(url):
            return False
    return True


def _risk_item(name, loinc, value, unit, status="normal", reasoning="ok"):
    return RiskFlaggedValue(test_name=name, loinc_code=loinc, value=value,
                            unit=unit, status=status, reasoning=reasoning)


def _evil_explanation_item(name):
    """LLM output that tries hard to smuggle a fabricated citation in."""
    return {
        "test_name": name,
        "explanation": "Results in this range are generally considered normal.",
        "doctor_questions": ["Should this be re-checked?"],
        "citation": f"Trusted Medical Source ({EVIL_URL})",
        "citation_url": EVIL_URL,          # must be stripped, never trusted
        "citation_status": "available",    # must be overwritten
    }


# ============================================================
# Test 1 — Successful URL response
# ============================================================

def test_successful_url_response_parsed():
    # Shape per project expectation: list of dicts with "url"
    with _urlopen_returning([{"url": GLUCOSE_URL}]):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is True
    assert result.url == GLUCOSE_URL
    assert GLUCOSE_URL in citation
    assert derive_citation_fields(citation) == (GLUCOSE_URL, "available")

    # Original feed shape keeps working (regression)
    with _urlopen_returning(_feed("Glucose - Blood Test", GLUCOSE_URL)):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is True
    assert result.title == "Glucose - Blood Test"
    assert citation == f"MedlinePlus: Glucose - Blood Test ({GLUCOSE_URL})"
    assert derive_citation_fields(citation) == (GLUCOSE_URL, "available")
    print("  PASS: successful URL response parsed (list + feed shapes)")


# ============================================================
# Test 2 — Multiple returned records
# ============================================================

def test_multiple_records_first_usable_wins():
    first = "https://medlineplus.gov/first-page"
    second = "https://medlineplus.gov/second-page"
    # Existing project behavior: the first relevant entry wins
    with _urlopen_returning([{"url": first}, {"url": second}]):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is True
    assert result.url == first
    assert first in citation
    assert second not in citation

    # Unusable leading records are skipped; a later usable one wins
    with _urlopen_returning([{"foo": "bar"}, {"url": second}]):
        result = fetch_medlineplus_info("2345-7")
    assert result.found is True
    assert result.url == second
    print("  PASS: multiple records handled per existing behavior (first usable)")


# ============================================================
# Test 3 — Empty response
# ============================================================

def test_empty_response_citation_unavailable():
    # Mapped LOINC: empty live response -> deterministic fallback page only
    with _urlopen_returning([]):
        mapped_citation = get_citation("2345-7", "Glucose")
    assert "Glucose" in mapped_citation
    assert _only_trusted_urls(mapped_citation)

    # Unmapped LOINC: explicit unavailable state, no URL whatsoever
    with _urlopen_returning([]):
        citation = get_citation(UNKNOWN_LOINC, UNKNOWN_NAME)
    assert CITATION_UNAVAILABLE_MARKER in citation
    assert "http" not in citation
    assert derive_citation_fields(citation) == (None, "unavailable")

    # JSON null payload is likewise controlled
    with _urlopen_returning(None):
        citation = get_citation(UNKNOWN_LOINC, UNKNOWN_NAME)
    assert CITATION_UNAVAILABLE_MARKER in citation
    assert "http" not in citation
    print("  PASS: empty response -> citation unavailable, nothing fabricated")


# ============================================================
# Test 4 — Malformed response
# ============================================================

def test_malformed_responses_controlled():
    malformed_payloads = [
        None,                # JSON null
        {},                  # empty object
        [{}],                # list with empty record
        [{"foo": "bar"}],    # record without any citation fields
        42,                  # scalar
        "unexpected-string",
        b"not valid json {{{",  # invalid JSON bytes
    ]
    for payload in malformed_payloads:
        with _urlopen_returning(payload):
            result = fetch_medlineplus_info("2345-7")
            citation = get_citation("2345-7", "Glucose")
        assert result.found is False, f"payload {payload!r} must not be 'found'"
        assert result.error, f"payload {payload!r} must report a controlled error"
        assert _only_trusted_urls(citation), \
            f"payload {payload!r} leaked an untrusted URL: {citation}"

    # A direct {"url": ...} record IS a recognized shape and still parses
    with _urlopen_returning({"url": GLUCOSE_URL}):
        result = fetch_medlineplus_info("2345-7")
    assert result.found is True
    assert result.url == GLUCOSE_URL
    print("  PASS: malformed responses handled without crashing")


# ============================================================
# Test 5 — Invalid/untrusted URL
# ============================================================

def test_untrusted_url_rejected():
    with _urlopen_returning([{"url": EVIL_URL}]):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is False, "untrusted URL must not yield a found result"
    assert EVIL_URL not in citation
    assert "evil.example.com" not in citation
    # Falls back to the deterministic mapped page or explicit unavailable —
    # never the untrusted domain.
    assert _only_trusted_urls(citation)
    assert citation.startswith("MedlinePlus:") or CITATION_UNAVAILABLE_MARKER in citation

    # Title present but link untrusted: title may be cited, URL may not pass
    with _urlopen_returning({"title": "Some Page", "url": EVIL_URL}):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert "evil.example.com" not in citation
    assert _only_trusted_urls(citation)

    # URL trust policy — accept real MedlinePlus, reject every trick
    for good in (
        "https://medlineplus.gov/lab-tests/x/",
        "https://www.medlineplus.gov/",
        "https://connect.medlineplus.gov/service",
        "http://medlineplus.gov/legacy",
    ):
        assert is_trusted_medlineplus_url(good), good
    for bad in (
        "https://medlineplus.gov.evil.com/phish",   # suffix trick
        "https://evil.com/medlineplus.gov",         # path trick
        "https://medlineplus.gov@evil.com/",        # userinfo trick
        "https://user@medlineplus.gov/",            # embedded credentials
        "javascript:alert(1)",
        "ftp://medlineplus.gov/file",
        "not a url at all",
        "",
        None,
        123,
    ):
        assert not is_trusted_medlineplus_url(bad), bad
    print("  PASS: untrusted URLs rejected by the citation trust policy")


# ============================================================
# Test 6 — Timeout
# ============================================================

def test_timeout_controlled():
    import socket
    import urllib.error
    for exc in (socket.timeout("timed out"),
                urllib.error.URLError(socket.timeout("timed out"))):
        with _urlopen_raising(exc):
            result = fetch_medlineplus_info("2345-7")
            citation = get_citation("2345-7", "Glucose")
        assert result.found is False
        assert result.error is not None and "Timeout" in result.error
        # no crash, no fabricated citation: only the deterministic fallback
        # page (trusted) or the explicit unavailable state
        assert _only_trusted_urls(citation)
        assert citation.startswith("MedlinePlus:") or CITATION_UNAVAILABLE_MARKER in citation

    # unmapped LOINC + timeout -> explicit unavailable, zero URLs
    with _urlopen_raising(socket.timeout("timed out")):
        citation = get_citation(UNKNOWN_LOINC, UNKNOWN_NAME)
    assert CITATION_UNAVAILABLE_MARKER in citation
    assert "http" not in citation
    print("  PASS: timeout handled as controlled failure, no fabricated citation")


# ============================================================
# Test 7 — Network exception
# ============================================================

def test_network_error_controlled():
    import urllib.error
    with _urlopen_raising(urllib.error.URLError("Connection refused")):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is False
    assert result.error is not None and "Network error" in result.error
    assert _only_trusted_urls(citation)
    assert citation.startswith("MedlinePlus:") or CITATION_UNAVAILABLE_MARKER in citation

    with _urlopen_raising(urllib.error.URLError("Connection refused")):
        citation = get_citation(UNKNOWN_LOINC, UNKNOWN_NAME)
    assert CITATION_UNAVAILABLE_MARKER in citation
    assert "http" not in citation
    print("  PASS: network failure handled as controlled failure")


# ============================================================
# Test 8 — Unexpected exception
# ============================================================

def test_unexpected_exception_controlled():
    with _urlopen_raising(RuntimeError("boom — client blew up")):
        result = fetch_medlineplus_info("2345-7")
        citation = get_citation("2345-7", "Glucose")
    assert result.found is False
    assert result.error is not None and "Unexpected error" in result.error
    assert "RuntimeError" in result.error  # debuggable, not swallowed
    assert _only_trusted_urls(citation)
    assert citation.startswith("MedlinePlus:") or CITATION_UNAVAILABLE_MARKER in citation
    print("  PASS: unexpected client exception contained with a debuggable error")


# ============================================================
# Test 9 — Unknown/unsupported LOINC
# ============================================================

def test_unknown_loinc_no_fabrication():
    # No LOINC at all + unknown name: no HTTP attempt, explicit unavailable
    with patch("tools.medlineplus_connect.urllib.request.urlopen",
               side_effect=AssertionError("HTTP must not be attempted")):
        citation = get_citation("", "NotARealTest")
    assert citation == f"NotARealTest — {CITATION_UNAVAILABLE_MARKER}"
    assert "http" not in citation

    # Unknown LOINC over the wire: service no-match -> explicit unavailable
    with _urlopen_returning({"feed": {"entry": []}}):
        citation = get_citation(UNKNOWN_LOINC, UNKNOWN_NAME)
    assert CITATION_UNAVAILABLE_MARKER in citation
    assert "http" not in citation
    assert derive_citation_fields(citation) == (None, "unavailable")
    print("  PASS: unknown LOINC never produces a fabricated citation")


# ============================================================
# Test 10 — Citation propagation (lookup -> explanation input AND output)
# ============================================================

def test_citation_propagates_to_explanation():
    risk = [_risk_item("Glucose", "2345-7", 92.0, "mg/dL")]
    llm = _llm_returning_items([_evil_explanation_item("Glucose")])

    with _urlopen_returning(_feed("Glucose - Blood Test", GLUCOSE_URL)):
        expected = get_citation("2345-7", "Glucose")  # trusted reference value
        with patch("agents.explainer.call_model", llm), \
             patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())):
            explanations = asyncio.run(explain(risk))

    exp = explanations[0]
    # Explanation INPUT carried the trusted citation from the lookup layer:
    ctx = _payload_after(llm, "Explain these lab results:\n")
    assert ctx[0]["citation"] == expected
    assert ctx[0]["citation_url"] == GLUCOSE_URL
    assert ctx[0]["citation_status"] == "available"
    assert ctx[0]["loinc_code"] == "2345-7"  # identity tied to the lookup
    # Explanation OUTPUT uses the SAME validated citation — the LLM's evil
    # citation and injected grounding fields are discarded:
    assert exp.citation == expected
    assert exp.citation_url == GLUCOSE_URL
    assert exp.citation_status == "available"
    assert EVIL_URL not in exp.citation
    print("  PASS: validated citation propagates lookup -> explanation in/out")


# ============================================================
# Test 11 — No-citation propagation (failure -> explicit unavailable)
# ============================================================

def test_no_citation_propagates_as_unavailable():
    import urllib.error
    risk = [_risk_item(UNKNOWN_NAME, UNKNOWN_LOINC, 42.0, "U", status="unavailable",
                       reasoning="no range")]
    llm = _llm_returning_items([_evil_explanation_item(UNKNOWN_NAME)])

    with _urlopen_raising(urllib.error.URLError("service down")):
        with patch("agents.explainer.call_model", llm), \
             patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())):
            explanations = asyncio.run(explain(risk))

    exp = explanations[0]
    # Input explicitly told the LLM no citation exists:
    ctx = _payload_after(llm, "Explain these lab results:\n")
    assert ctx[0]["citation_status"] == "unavailable"
    assert ctx[0]["citation_url"] is None
    assert CITATION_UNAVAILABLE_MARKER in ctx[0]["citation"]
    # Output has no made-up URL, and the schema state is explicit:
    assert exp.citation_url is None
    assert exp.citation_status == "unavailable"
    assert CITATION_UNAVAILABLE_MARKER in exp.citation
    assert "evil.example.com" not in exp.citation
    print("  PASS: failed lookup propagates explicit unavailable state, no URL")


def test_llm_invented_test_name_gets_no_citation_state():
    """An LLM-invented test_name has no trusted lookup → explicit no-citation."""
    risk = [_risk_item("Glucose", "2345-7", 92.0, "mg/dL")]
    invented = _evil_explanation_item("Totally Made Up Panel")
    llm = _llm_returning_items([invented])

    with _urlopen_returning(_feed("Glucose - Blood Test", GLUCOSE_URL)):
        with patch("agents.explainer.call_model", llm), \
             patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())):
            explanations = asyncio.run(explain(risk))

    exp = explanations[0]
    assert exp.citation == f"Totally Made Up Panel — {CITATION_UNAVAILABLE_MARKER}"
    assert exp.citation_url is None
    assert exp.citation_status == "unavailable"
    assert EVIL_URL not in exp.citation
    print("  PASS: LLM-invented test_name can never receive a citation")


# ============================================================
# Test 12 — Citation isolation between lab results
# ============================================================

def test_citation_isolation_between_loinCs():
    risk = [
        _risk_item("Glucose", "2345-7", 92.0, "mg/dL"),
        _risk_item("WBC count", "6690-2", 6.5, "K/uL"),
    ]
    # LLM deliberately returns the citations SWAPPED to try to leak one
    # lab's source into the other's explanation:
    llm = _llm_returning_items([
        {**_evil_explanation_item("Glucose"), "citation": f"Source ({WBC_URL})"},
        {**_evil_explanation_item("WBC count"), "citation": f"Source ({GLUCOSE_URL})"},
    ])

    with _urlopen_dispatch({
        "2345-7": _feed("Glucose - Blood Test", GLUCOSE_URL),
        "6690-2": _feed("White Blood Cell Count", WBC_URL),
    }):
        with patch("agents.explainer.call_model", llm), \
             patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())):
            explanations = asyncio.run(explain(risk))

    exp_glu, exp_wbc = explanations
    assert exp_glu.citation_url == GLUCOSE_URL
    assert exp_wbc.citation_url == WBC_URL
    assert exp_glu.citation == f"MedlinePlus: Glucose - Blood Test ({GLUCOSE_URL})"
    assert exp_wbc.citation == f"MedlinePlus: White Blood Cell Count ({WBC_URL})"
    assert WBC_URL not in exp_glu.citation   # no cross-contamination
    assert GLUCOSE_URL not in exp_wbc.citation
    print("  PASS: citations are isolated per lab identity (no leakage)")


def test_all_supported_loinc_codes_have_controlled_citations():
    """Every mapped LOINC yields a controlled, trusted-or-unavailable citation."""
    mapping_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "medlineplus_mapping.json")
    with open(mapping_path, "r") as fh:
        codes = list(json.load(fh)["mappings"].keys())
    assert len(codes) >= 20, "mapping unexpectedly small"

    from tools.medlineplus_connect import LOINC_OID, build_connect_url
    for loinc in codes:
        # Request construction is well-formed for every supported code
        request_url = build_connect_url(loinc)
        assert LOINC_OID in request_url
        assert f"mainSearchCriteria.v.c={loinc}" in request_url
        # Service-side no-match must still be a controlled citation outcome
        with _urlopen_returning({"feed": {"entry": []}}):
            citation = get_citation(loinc, f"Panel {loinc}")
        assert _only_trusted_urls(citation), f"{loinc} leaked untrusted URL: {citation}"
        assert citation.startswith("MedlinePlus:") or CITATION_UNAVAILABLE_MARKER in citation, \
            loinc
    print(f"  PASS: all {len(codes)} supported LOINCs produce controlled citations")


# ============================================================
# Integration (Step 9) — mocked end-to-end citation flow
# ============================================================

def test_integration_lookup_risk_explain_flow():
    """lab result -> reference/risk pipeline -> MedlinePlus lookup -> explain."""
    from agents.risk_flagger import classify_risk

    extracted = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="WBC count", loinc_code="6690-2", value=6.5, unit="K/uL"),
    ]
    checked = lookup_ranges(extracted)  # real deterministic local lookup
    assert all(c.range_available for c in checked)

    risk_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0,
         "unit": "mg/dL", "status": "normal", "reasoning": "in range"},
        # identity must match the POST-conversion lookup output (P0-T2 rule)
        {"test_name": "WBC count", "loinc_code": "6690-2", "value": 6500.0,
         "unit": "cells/mcL", "status": "normal", "reasoning": "in range"},
    ])
    with patch("agents.risk_flagger.call_model", risk_llm), \
         patch("agents.risk_flagger.get_client", MagicMock(return_value=MagicMock())):
        risk = asyncio.run(classify_risk(checked))
    assert [r.loinc_code for r in risk] == ["2345-7", "6690-2"]

    explainer_llm = _llm_returning_items([
        _evil_explanation_item("Glucose"),
        _evil_explanation_item("WBC count"),
    ])
    with _urlopen_dispatch({
        "2345-7": _feed("Glucose - Blood Test", GLUCOSE_URL),
        "6690-2": _feed("White Blood Cell Count", WBC_URL),
    }):
        with patch("agents.explainer.call_model", explainer_llm), \
             patch("agents.explainer.get_client", MagicMock(return_value=MagicMock())):
            explanations = asyncio.run(explain(risk))

    # The citation handed to the explanation stage is the validated per-LOINC
    # lookup result for THAT lab — nothing invented, nothing crossed over:
    assert explanations[0].citation_url == GLUCOSE_URL
    assert explanations[1].citation_url == WBC_URL
    assert explanations[0].citation_status == "available"
    assert explanations[1].citation_status == "available"
    for exp in explanations:
        assert EVIL_URL not in exp.citation
        assert "evil.example.com" not in exp.citation
    print("  PASS: integration flow grounds each explanation in its own lookup")


def test_pipeline_end_to_end_citation_grounding():
    """Full orchestrator run (PDF -> ... -> verify) with everything mocked.

    Proves the citation hardening survives every seam, including the
    verifier and the final serialized output — with zero network access.
    """
    from pipeline.orchestrator import run_pipeline

    sample_pdf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "samples", "normal_report.pdf")
    extract_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0, "unit": "mg/dL"},
    ])
    risk_llm = _llm_returning_items([
        {"test_name": "Glucose", "loinc_code": "2345-7", "value": 92.0,
         "unit": "mg/dL", "status": "normal", "reasoning": "92 within 70-100"},
    ])
    explainer_llm = _llm_returning_items([_evil_explanation_item("Glucose")])
    verifier_llm = _llm_returning_text(json.dumps({"passed": True, "issues_found": []}))

    llm_by_module = {
        "agents.extraction": extract_llm,
        "agents.risk_flagger": risk_llm,
        "agents.explainer": explainer_llm,
        "agents.verifier": verifier_llm,
    }
    with ExitStack() as stack:
        for module, mock_llm in llm_by_module.items():
            stack.enter_context(patch(f"{module}.call_model", mock_llm))
            stack.enter_context(patch(f"{module}.get_client",
                                      MagicMock(return_value=MagicMock())))
        stack.enter_context(_urlopen_dispatch(
            {"2345-7": _feed("Glucose - Blood Test", GLUCOSE_URL)}))
        result = asyncio.run(run_pipeline(sample_pdf))

    assert result["verified"] is True
    exp = result["explanations"][0]
    # Serialized final output keeps the schema contract and the grounding:
    for key in ("test_name", "explanation", "doctor_questions", "citation",
                "citation_url", "citation_status"):
        assert key in exp, f"final output missing {key}"
    assert exp["citation_url"] == GLUCOSE_URL
    assert exp["citation_status"] == "available"
    assert GLUCOSE_URL in exp["citation"]
    assert EVIL_URL not in exp["citation"]
    print("  PASS: full pipeline output carries the validated citation only")


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
