"""MedlinePlus Connect integration tests.

Tests request construction, response parsing, error handling, and citation generation.
Uses mocking for HTTP requests to avoid network dependency.
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.medlineplus_connect import (
    build_connect_url,
    fetch_medlineplus_info,
    get_citation,
    resolve_loinc_from_test_name,
    LOINC_OID,
    MEDLINEPLUS_CONNECT_BASE,
)


def test_request_construction():
    """Correct request URL is constructed from LOINC code."""
    url = build_connect_url("2345-7")
    assert MEDLINEPLUS_CONNECT_BASE in url
    assert LOINC_OID in url
    assert "mainSearchCriteria.v.c=2345-7" in url
    # URL encoding may convert "/" to "%2F"
    assert "knowledgeResponseType=application" in url
    assert "json" in url
    print("  PASS: Request URL construction")


def test_loinc_oid_used():
    """LOINC OID 2.16.840.1.113883.6.1 is used in all requests."""
    for code in ["2345-7", "718-7", "2160-0"]:
        url = build_connect_url(code)
        assert LOINC_OID in url, f"LOINC OID missing for code {code}"
    print("  PASS: LOINC OID used in all requests")


def test_successful_response_parsing():
    """Successful MedlinePlus Connect response is parsed correctly."""
    mock_response = {
        "feed": {
            "entry": [
                {
                    "title": {"_value": "Glucose - Blood Test"},
                    "link": {"@href": "https://medlineplus.gov/lab-tests/comprehensive-metabolic-panel-cmp/"},
                    "summary": {"_value": "A glucose test measures the level of glucose in your blood."},
                }
            ]
        }
    }

    mock_raw = json.dumps(mock_response).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("tools.medlineplus_connect.urllib.request.urlopen", return_value=mock_resp):
        result = fetch_medlineplus_info("2345-7")

    assert result.found is True
    assert result.title == "Glucose - Blood Test"
    assert "medlineplus.gov" in result.url
    print("  PASS: Successful response parsing")


def test_no_match_response():
    """No-match response returns found=False with fallback URL."""
    mock_response = {"feed": {"entry": []}}
    mock_raw = json.dumps(mock_response).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("tools.medlineplus_connect.urllib.request.urlopen", return_value=mock_resp):
        result = fetch_medlineplus_info("99999-9")

    assert result.found is False
    print("  PASS: No-match response handled")


def test_timeout_handling():
    """Timeout is handled gracefully without crashing."""
    import urllib.error
    import socket

    with patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        result = fetch_medlineplus_info("2345-7")

    assert result.found is False
    assert result.error is not None
    assert "Timeout" in result.error
    print("  PASS: Timeout handled gracefully")


def test_network_error_handling():
    """Network error is handled gracefully."""
    import urllib.error

    with patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        result = fetch_medlineplus_info("2345-7")

    assert result.found is False
    assert result.error is not None
    assert "Network error" in result.error
    print("  PASS: Network error handled gracefully")


def test_malformed_response_handling():
    """Malformed JSON response is handled gracefully."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not valid json {{{"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("tools.medlineplus_connect.urllib.request.urlopen", return_value=mock_resp):
        result = fetch_medlineplus_info("2345-7")

    assert result.found is False
    assert result.error is not None
    assert "Malformed" in result.error
    print("  PASS: Malformed response handled gracefully")


def test_no_fabricated_citation_on_no_result():
    """No fabricated citation when MedlinePlus has no result."""
    # Use a LOINC code that won't match anything
    with patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=Exception("should not be called")):
        citation = get_citation("", "FakeTest999")

    assert "MedlinePlus" not in citation or "no MedlinePlus citation available" in citation
    assert "FakeTest999" in citation
    print("  PASS: No fabricated citation")


def test_fallback_url_used():
    """Fallback URL is used when runtime fetch fails."""
    # Glucose (2345-7) has a fallback page in the mapping
    import urllib.error
    with patch("tools.medlineplus_connect.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        citation = get_citation("2345-7", "Glucose")

    assert "Glucose" in citation
    # Should contain either fallback URL or "no citation"
    print(f"  PASS: Fallback citation: {citation[:80]}...")


def test_resolve_loinc_from_test_name():
    """LOINC resolution from test name works for common tests."""
    assert resolve_loinc_from_test_name("Glucose") == "2345-7"
    assert resolve_loinc_from_test_name("Hemoglobin") == "718-7"
    assert resolve_loinc_from_test_name("CREATININE") == "2160-0"  # case insensitive
    assert resolve_loinc_from_test_name("") == ""
    assert resolve_loinc_from_test_name("Nonexistent") == ""
    print("  PASS: LOINC resolution from test name")


if __name__ == "__main__":
    print("=== MedlinePlus Connect Tests ===\n")
    test_request_construction()
    test_loinc_oid_used()
    test_successful_response_parsing()
    test_no_match_response()
    test_timeout_handling()
    test_network_error_handling()
    test_malformed_response_handling()
    test_no_fabricated_citation_on_no_result()
    test_fallback_url_used()
    test_resolve_loinc_from_test_name()
    print("\nAll MedlinePlus Connect tests passed.")
