"""MedlinePlus Connect client — fetches patient-friendly info by LOINC code.

Uses the official MedlinePlus Connect web service:
  https://connect.medlineplus.gov/service

LOINC OID: 2.16.840.1.113883.6.1

This is NOT a reference-range database.
This provides educational grounding for explanations.
"""

import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
import socket
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

MAPPING_PATH = Path(__file__).resolve().parent.parent / "data" / "medlineplus_mapping.json"
SUPPORTED_LABS_PATH = Path(__file__).resolve().parent.parent / "data" / "supported_labs.json"

# Service configuration — no API key required for public endpoint
MEDLINEPLUS_CONNECT_BASE = "https://connect.medlineplus.gov/service"
LOINC_OID = "2.16.840.1.113883.6.1"
REQUEST_TIMEOUT = 10  # seconds

# Load mapping once at module level
_mapping_data: dict = None
_supported_labs_by_name: dict = None


def _load_mapping() -> dict:
    """Load medlineplus_mapping.json (cached)."""
    global _mapping_data
    if _mapping_data is None:
        with open(MAPPING_PATH, "r") as f:
            raw = json.load(f)
        _mapping_data = raw.get("mappings", {})
    return _mapping_data


def _load_supported_labs_by_name() -> dict:
    """Load supported_labs.json indexed by test_name (cached)."""
    global _supported_labs_by_name
    if _supported_labs_by_name is None:
        with open(SUPPORTED_LABS_PATH, "r") as f:
            raw = json.load(f)
        _supported_labs_by_name = {}
        for test in raw.get("tests", []):
            _supported_labs_by_name[test["test_name"].lower()] = test["loinc_code"]
    return _supported_labs_by_name


def resolve_loinc_from_test_name(test_name: str) -> str:
    """Resolve a LOINC code from a test name using supported_labs.json."""
    labs = _load_supported_labs_by_name()
    return labs.get(test_name.lower(), "")


@dataclass
class MedlinePlusResult:
    """Result from MedlinePlus Connect lookup."""
    loinc_code: str
    found: bool
    title: Optional[str] = None
    url: Optional[str] = None
    summary: Optional[str] = None
    fallback_url: Optional[str] = None
    error: Optional[str] = None


def build_connect_url(loinc_code: str) -> str:
    """Build a MedlinePlus Connect request URL for a LOINC code.

    Uses the LOINC OID (2.16.840.1.113883.6.1) as required.
    """
    params = {
        "mainSearchCriteria.v.cs": LOINC_OID,
        "mainSearchCriteria.v.c": loinc_code,
        "informationRecipient.languageCode.c": "en",
        "knowledgeResponseType": "application/json",
    }
    return f"{MEDLINEPLUS_CONNECT_BASE}?{urllib.parse.urlencode(params)}"


def fetch_medlineplus_info(loinc_code: str, timeout: int = REQUEST_TIMEOUT) -> MedlinePlusResult:
    """Fetch MedlinePlus information for a given LOINC code.

    Handles:
    - Successful match -> returns title, URL, summary
    - No match -> found=False with fallback_url if available
    - Timeout -> error set, fallback_url if available
    - Network error -> error set, fallback_url if available
    - Malformed response -> error set, fallback_url if available
    """
    mapping = _load_mapping()
    entry = mapping.get(loinc_code)

    fallback_url = None
    if entry:
        fallback_url = entry.get("known_human_readable_fallback_page")

    # Check if this mapping is fallback-only (no runtime resolution expected)
    if entry and entry.get("fallback_only", False):
        # Still attempt runtime fetch, but fallback is the expected path
        pass

    url = build_connect_url(loinc_code)

    try:
        # Create SSL context that works on Windows
        ctx = ssl.create_default_context()

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "MedReportAI/0.1-demo",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            raw_data = response.read().decode("utf-8")
            data = json.loads(raw_data)

        # Parse MedlinePlus Connect response
        # The response typically contains a feed with entries
        if not data:
            return MedlinePlusResult(
                loinc_code=loinc_code,
                found=False,
                fallback_url=fallback_url,
                error="Empty response from MedlinePlus Connect",
            )

        # Try to extract useful information from the response
        # MedlinePlus Connect returns a JSON feed
        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            # Try alternative response structures
            entries = data.get("entries", [])
        if not entries:
            entries = data.get("result", {}).get("resources", []) if isinstance(data.get("result"), dict) else []

        if entries:
            # Take the first relevant entry
            entry = entries[0] if isinstance(entries, list) else entries

            # Extract title — handle dict with _value, string, or other
            raw_title = entry.get("title", "")
            if isinstance(raw_title, dict):
                title = raw_title.get("_value", "")
            elif isinstance(raw_title, str):
                title = raw_title
            else:
                title = str(raw_title) if raw_title else ""

            # Extract link — handle dict with @href, list of dicts, or string
            raw_link = entry.get("link", "")
            if isinstance(raw_link, dict):
                link = raw_link.get("@href", "")
            elif isinstance(raw_link, list) and raw_link:
                # List of link dicts — take first one with href
                first = raw_link[0]
                link = first.get("href", first.get("@href", "")) if isinstance(first, dict) else str(first)
            elif isinstance(raw_link, str):
                link = raw_link
            else:
                link = ""

            # Extract summary — handle dict with _value, string, or other
            raw_summary = entry.get("summary", "")
            if isinstance(raw_summary, dict):
                summary = raw_summary.get("_value", "")
            elif isinstance(raw_summary, str):
                summary = raw_summary
            else:
                summary = str(raw_summary) if raw_summary else ""

            if title or link:
                return MedlinePlusResult(
                    loinc_code=loinc_code,
                    found=True,
                    title=title or None,
                    url=link or None,
                    summary=summary or None,
                    fallback_url=fallback_url,
                )

        # No usable entries found
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
        )

    except urllib.error.URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=f"Network error: {reason}",
        )
    except socket.timeout:
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=f"Timeout after {timeout}s",
        )
    except json.JSONDecodeError as e:
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=f"Malformed JSON response: {e}",
        )
    except Exception as e:
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=f"Unexpected error: {type(e).__name__}: {e}",
        )


def get_citation(loinc_code: str, test_name: str) -> str:
    """Get a citation string for a lab test.

    Priority:
    1. MedlinePlus Connect result title + URL
    2. Fallback page URL from mapping
    3. "[test_name] — no MedlinePlus citation available"

    Does NOT fabricate citations.
    """
    # Resolve LOINC if not provided
    if not loinc_code:
        loinc_code = resolve_loinc_from_test_name(test_name)

    if not loinc_code:
        return f"{test_name} — no MedlinePlus citation available"

    result = fetch_medlineplus_info(loinc_code)

    if result.found and result.title:
        if result.url:
            return f"MedlinePlus: {result.title} ({result.url})"
        return f"MedlinePlus: {result.title}"

    if result.fallback_url:
        return f"MedlinePlus: {test_name} ({result.fallback_url})"

    # No MedlinePlus result at all — do NOT fabricate
    return f"{test_name} — no MedlinePlus citation available"
