"""Live MedlinePlus Connect integration test — external network required.

Disabled by default so the normal deterministic suite never depends on live
MedlinePlus (P0-T6). Enable explicitly:

    MEDLINEPLUS_LIVE=1 python -m pytest tests/test_medlineplus_live.py -v

If the service is unreachable from the environment, the test reports itself
as blocked (skipped) — it never pretends to have passed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools.medlineplus_connect import (
    fetch_medlineplus_info,
    is_trusted_medlineplus_url,
)

LIVE_ENABLED = os.environ.get("MEDLINEPLUS_LIVE") == "1"


@pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="Live MedlinePlus test disabled — set MEDLINEPLUS_LIVE=1 to run "
           "(the normal suite must not depend on external network)",
)
def test_live_medlineplus_known_loinc():
    """A known supported LOINC returns a usable, trusted MedlinePlus result."""
    # 2345-7 = Glucose — a core code with a long-standing MedlinePlus page.
    result = fetch_medlineplus_info("2345-7", timeout=10)

    if result.error and any(
        key in result.error for key in ("Timeout", "Network", "Unexpected", "Malformed")
    ):
        pytest.skip(f"Live MedlinePlus unreachable — blocked, not passed: {result.error}")

    assert result.found is True, \
        f"MedlinePlus returned no match for known LOINC 2345-7 (error={result.error})"
    assert result.url, \
        "Expected a usable URL for the known LOINC — response shape may have changed"
    assert is_trusted_medlineplus_url(result.url), result.url
    assert result.url.startswith("https://")
    # Deliberately NOT asserting titles/summaries — external details may
    # change without this test needing to change.


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
