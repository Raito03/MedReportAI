"""MedlinePlus Connect client — fetches patient-friendly info by LOINC code.

Uses the official MedlinePlus Connect web service:
  https://connect.medlineplus.gov/service

LOINC OID: 2.16.840.1.113883.6.1

This is NOT a reference-range database.
This provides educational grounding for explanations.
"""

import json
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import socket
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

MAPPING_PATH = Path(__file__).resolve().parent.parent / "data" / "medlineplus_mapping.json"
SUPPORTED_LABS_PATH = Path(__file__).resolve().parent.parent / "data" / "supported_labs.json"

# Service configuration — no API key required for public endpoint
MEDLINEPLUS_CONNECT_BASE = "https://connect.medlineplus.gov/service"
LOINC_OID = "2.16.840.1.113883.6.1"
REQUEST_TIMEOUT = 10  # seconds

# --- P0-T6 citation trust policy -----------------------------------------
# Only URLs on medlineplus.gov (or its subdomains) may ever be presented as a
# trusted citation. Anything else — arbitrary domains, scheme tricks,
# userinfo tricks — is rejected and never shown to the user as a source.
TRUSTED_HOST_SUFFIX = "medlineplus.gov"

# Marker present in the explicit no-citation state produced by get_citation().
CITATION_UNAVAILABLE_MARKER = "no MedlinePlus citation available"


def is_trusted_medlineplus_url(url: Any) -> bool:
    """True iff `url` is an acceptable MedlinePlus citation destination.

    Accepts only http(s) URLs whose host is exactly `medlineplus.gov` or a
    subdomain of it, with no embedded credentials. Used to validate both
    URLs returned by MedlinePlus Connect and the project's deterministic
    fallback pages before they are shown as citations.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.username or parts.password:
        # e.g. https://medlineplus.gov@evil.example/ — classic trust trick
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return host == TRUSTED_HOST_SUFFIX or host.endswith("." + TRUSTED_HOST_SUFFIX)


def derive_citation_fields(citation: Any) -> "tuple[Optional[str], str]":
    """Derive (citation_url, citation_status) from a citation string.

    The citation string is produced by get_citation() — the trusted lookup
    layer — so this is used to attach the explicit structured state to the
    explanation schema. Never used to *validate* LLM-supplied text.

    Returns:
        (validated_url_or_None, "available" | "unavailable")
    """
    if not citation or not isinstance(citation, str):
        return None, "unavailable"
    if CITATION_UNAVAILABLE_MARKER in citation:
        return None, "unavailable"
    match = re.search(r"\((https?://[^)\s]+)\)", citation)
    if match and is_trusted_medlineplus_url(match.group(1)):
        return match.group(1), "available"
    # A citation exists (e.g. title-only MedlinePlus result) but carries no
    # validated URL.
    return None, "available"


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


def _value_or_text(raw: Any) -> str:
    """Extract text from a MedlinePlus field (str, {"_value": ...}, or other)."""
    if isinstance(raw, dict):
        return str(raw.get("_value", "") or "")
    if isinstance(raw, str):
        return raw
    return str(raw) if raw else ""


def _extract_link(record: dict) -> str:
    """Extract a URL candidate from one response record.

    Supports the shapes the project expects: link dicts with @href/href,
    lists of link dicts, plain strings, and simple records such as
    {"url": "https://medlineplus.gov/..."}.
    """
    for key in ("link", "url", "href"):
        if key not in record:
            continue
        raw = record[key]
        if isinstance(raw, dict):
            candidate = raw.get("@href") or raw.get("href") or ""
        elif isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict):
                candidate = first.get("@href") or first.get("href") or ""
            else:
                candidate = str(first)
        elif isinstance(raw, str):
            candidate = raw
        else:
            candidate = ""
        if candidate:
            return candidate
    return ""


def _extract_records(data: Any) -> "tuple[List[dict], bool]":
    """Normalize a MedlinePlus Connect response into a list of records.

    Returns (records, recognized):
      - recognized=True  → the response shape is one the project understands
        (feed/entries/result containers, a single record dict, or a list of
        records). A recognized response may legitimately hold zero records
        (that is a no-match, not an error).
      - recognized=False → unrecognized format; caller fails controlled.
    """
    if isinstance(data, dict):
        has_container = any(k in data for k in ("feed", "entries", "result"))
        has_record_keys = any(k in data for k in ("link", "url", "href", "title"))
        if not has_container and not has_record_keys:
            return [], False

        entries: List[Any] = []
        feed = data.get("feed")
        if isinstance(feed, dict):
            entries = feed.get("entry", []) or []
        if not entries and isinstance(data.get("entries"), list):
            entries = data["entries"]
        if not entries and isinstance(data.get("result"), dict):
            entries = data["result"].get("resources", []) or []
        if not entries and has_record_keys and not has_container:
            entries = [data]  # single record returned directly

        if not isinstance(entries, list):
            entries = [entries] if isinstance(entries, dict) else []
        return [e for e in entries if isinstance(e, dict)], True

    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)], True

    return [], False


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
        candidate = entry.get("known_human_readable_fallback_page")
        # Defense in depth: even the project's own mapping file must contain
        # a trusted medlineplus.gov URL before it may be shown as a citation.
        if is_trusted_medlineplus_url(candidate):
            fallback_url = candidate

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

        # Parse MedlinePlus Connect response — P0-T6 hardened.
        # Supported shapes: feed/entries/result containers (original format),
        # a single record dict, or a plain list of records such as
        # [{"url": "https://medlineplus.gov/..."}].
        if not data:
            return MedlinePlusResult(
                loinc_code=loinc_code,
                found=False,
                fallback_url=fallback_url,
                error="Empty response from MedlinePlus Connect",
            )

        records, recognized = _extract_records(data)
        if not recognized:
            return MedlinePlusResult(
                loinc_code=loinc_code,
                found=False,
                fallback_url=fallback_url,
                error="Unrecognized response format from MedlinePlus Connect",
            )

        saw_untrusted_url = False
        for record in records:
            title = _value_or_text(record.get("title", ""))
            summary = _value_or_text(record.get("summary", ""))
            raw_link = _extract_link(record)

            link = ""
            if raw_link:
                if is_trusted_medlineplus_url(raw_link):
                    link = raw_link
                else:
                    # P0-T6: never pass an untrusted domain through as a
                    # trusted MedlinePlus citation.
                    saw_untrusted_url = True

            if title or link:
                return MedlinePlusResult(
                    loinc_code=loinc_code,
                    found=True,
                    title=title or None,
                    url=link or None,
                    summary=summary or None,
                    fallback_url=fallback_url,
                )

        # Recognized response but nothing usable:
        # - zero records → legitimate no-match (error stays None)
        # - records present → nothing citable; untrusted URLs are reported
        error = None
        if records:
            error = "No usable citation data in MedlinePlus response"
        if saw_untrusted_url:
            error = "Rejected untrusted URL from MedlinePlus response"
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=error,
        )

    except urllib.error.URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            error = f"Timeout after {timeout}s"
        else:
            error = f"Network error: {reason}"
        return MedlinePlusResult(
            loinc_code=loinc_code,
            found=False,
            fallback_url=fallback_url,
            error=error,
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

    Priority (P0-T6 hardened — validated MedlinePlus result, then the
    project's deterministic fallback page, then explicit unavailable):
    1. MedlinePlus Connect result (title and/or validated URL)
    2. Fallback page URL from the mapping — only if it validates as a
       trusted medlineplus.gov URL
    3. Explicit no-citation state: "[test_name] — no MedlinePlus citation
       available"

    Does NOT fabricate citations. Every URL in the returned string has been
    validated with is_trusted_medlineplus_url(); the LLM never generates it.
    """
    # Resolve LOINC if not provided
    if not loinc_code:
        loinc_code = resolve_loinc_from_test_name(test_name)

    if not loinc_code:
        return f"{test_name} — {CITATION_UNAVAILABLE_MARKER}"

    result = fetch_medlineplus_info(loinc_code)

    # 1) Validated MedlinePlus Connect result
    if result.found:
        url = result.url if is_trusted_medlineplus_url(result.url) else None
        if url and result.title:
            return f"MedlinePlus: {result.title} ({url})"
        if url:
            return f"MedlinePlus: {test_name} ({url})"
        if result.title:
            # Real title from the response but no validated URL available —
            # title-only citation, never an untrusted URL.
            return f"MedlinePlus: {result.title}"

    # 2) Deterministic fallback page from the mapping (validated again here)
    if is_trusted_medlineplus_url(result.fallback_url):
        return f"MedlinePlus: {test_name} ({result.fallback_url})"

    # 3) No MedlinePlus result at all — explicit unavailable state, do NOT fabricate
    return f"{test_name} — {CITATION_UNAVAILABLE_MARKER}"

