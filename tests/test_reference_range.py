"""Reference range lookup correctness tests.

Tests against the supplied data/reference_ranges.json (MedlinePlus intervals).
Covers: known LOINC, unknown LOINC, missing ranges, unit mismatch,
sex-specific ranges, boundary values, and the full supported_labs integration.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.reference_range import lookup_ranges, lookup_range_detail, _load_ranges, _load_supported_labs
from tools.medlineplus_connect import resolve_loinc_from_test_name, get_citation
from core.schemas import ExtractedLabValue


# ============================================================
# Reference Range Lookup Tests
# ============================================================

def test_known_loinc_glucose():
    """Known LOINC 2345-7 (Glucose) returns correct MedlinePlus range."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL")]
    result = lookup_ranges(values)
    assert len(result) == 1
    assert result[0].reference_low == 70.0
    assert result[0].reference_high == 100.0
    assert result[0].in_range is True
    print("  PASS: Known LOINC (Glucose)")


def test_known_loinc_hemoglobin():
    """Known LOINC 718-7 (Hemoglobin) — sex-specific ranges, no 'all' entry."""
    # Hemoglobin 718-7 only has male (13-18) and female (12-16) — no "all" entry
    # Without sex context, lookup should return range_available=False
    values = [ExtractedLabValue(test_name="Hemoglobin", loinc_code="718-7", value=14.0, unit="g/dL")]
    result = lookup_ranges(values)
    assert len(result) == 1
    # Since no "all" entry exists, ranges should be 0/0 (not available)
    assert result[0].reference_low == 0.0
    assert result[0].reference_high == 0.0
    assert result[0].in_range is False
    print("  PASS: Known LOINC (Hemoglobin) — sex-specific without context")


def test_known_loinc_all_common_tests():
    """Verify lookup works for all 27 LOINC codes in the dataset."""
    from agents.reference_range import RangeLookupInput
    ranges = _load_ranges()
    supported = _load_supported_labs()

    for loinc_code, entries in ranges.items():
        assert isinstance(entries, list), f"LOINC {loinc_code}: entries should be a list"
        assert len(entries) > 0, f"LOINC {loinc_code}: entries should not be empty"

        entry = entries[0]
        assert "reference_low" in entry, f"LOINC {loinc_code}: missing reference_low"
        assert "reference_high" in entry, f"LOINC {loinc_code}: missing reference_high"
        assert "unit" in entry, f"LOINC {loinc_code}: missing unit"
        assert "test_name" in entry, f"LOINC {loinc_code}: missing test_name"

        # Verify lookup works via detail API (which has range_available)
        params = RangeLookupInput(
            loinc_code=loinc_code,
            test_name=entry["test_name"],
            value=entry["reference_low"],
            unit=entry["unit"],
        )
        detail = lookup_range_detail(params)
        # Range should be available OR have a descriptive reason (e.g. sex-specific)
        assert detail.range_available or detail.range_note, f"LOINC {loinc_code}: should have range or reason"

    print(f"  PASS: All {len(ranges)} LOINC codes have valid structure")


def test_unknown_loinc_cannot_become_normal():
    """Unknown LOINC must NOT default to normal (in_range=True).

    This is a critical safety fix — old code returned 0/0/in_range=True for unknown LOINCs.
    """
    values = [ExtractedLabValue(test_name="Mystery Test", loinc_code="99999-9", value=42.0, unit="U")]
    result = lookup_ranges(values)
    assert len(result) == 1
    # in_range must be False for unknown LOINC
    assert result[0].in_range is False, "Unknown LOINC must NOT be marked as in_range"
    # reference ranges should be 0/0 (not available)
    assert result[0].reference_low == 0.0
    assert result[0].reference_high == 0.0
    print("  PASS: Unknown LOINC cannot become normal")


def test_unknown_loinc_detail():
    """Unknown LOINC returns controlled failure with descriptive note."""
    from agents.reference_range import RangeLookupInput
    params = RangeLookupInput(
        loinc_code="99999-9",
        test_name="Mystery Test",
        value=42.0,
        unit="U",
    )
    result = lookup_range_detail(params)
    assert result.range_available is False
    assert "not in the supported labs list" in result.range_note
    print("  PASS: Unknown LOINC detail has descriptive note")


def test_missing_loinc_in_supported_but_no_range():
    """Supported LOINC with no reference range returns controlled failure."""
    # RBC count (789-8) has sex-specific ranges — test with no sex context
    from agents.reference_range import RangeLookupInput
    params = RangeLookupInput(
        loinc_code="789-8",
        test_name="RBC count",
        value=5.0,
        unit="million cells/mcL",
    )
    result = lookup_range_detail(params)
    # Should have range_available=True because "all" entry exists for 789-8? No — 789-8 only has male/female
    # Actually checking: 789-8 has male (4.6-6.2) and female (4.2-5.4) — no "all"
    assert result.range_available is False, "RBC count without sex context should not be available"
    assert "Sex-specific" in result.range_note
    print("  PASS: Sex-specific LOINC without context returns controlled failure")


def test_unit_mismatch():
    """Unit mismatch returns controlled failure, not a silent comparison."""
    # Glucose reference is mg/dL — if extracted value is mmol/L, should fail
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=5.1, unit="mmol/L")]
    result = lookup_ranges(values)
    assert result[0].in_range is False
    # The detailed lookup should show unit mismatch
    from agents.reference_range import RangeLookupInput
    params = RangeLookupInput(
        loinc_code="2345-7",
        test_name="Glucose",
        value=5.1,
        unit="mmol/L",
    )
    detail = lookup_range_detail(params)
    assert detail.range_available is False
    assert "Unit mismatch" in detail.range_note
    print("  PASS: Unit mismatch returns controlled failure")


def test_boundary_values_lower():
    """Test exact lower boundary behavior (inclusive)."""
    # Glucose: 70-100 mg/dL
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=69.0, unit="mg/dL"),  # below
        ExtractedLabValue(test_name="Glucose2", loinc_code="2345-7", value=70.0, unit="mg/dL"),  # exact lower
        ExtractedLabValue(test_name="Glucose3", loinc_code="2345-7", value=100.0, unit="mg/dL"),  # exact upper
        ExtractedLabValue(test_name="Glucose4", loinc_code="2345-7", value=101.0, unit="mg/dL"),  # above
    ]
    result = lookup_ranges(values)
    assert result[0].in_range is False, "69 should be below range"
    assert result[1].in_range is True, "70 (exact lower) should be in range"
    assert result[2].in_range is True, "100 (exact upper) should be in range"
    assert result[3].in_range is False, "101 should be above range"
    print("  PASS: Boundary values (lower/upper inclusive)")


def test_below_range():
    """Value below reference range returns in_range=False."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=50.0, unit="mg/dL")]
    result = lookup_ranges(values)
    assert result[0].in_range is False
    print("  PASS: Below range")


def test_above_range():
    """Value above reference range returns in_range=False."""
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=200.0, unit="mg/dL")]
    result = lookup_ranges(values)
    assert result[0].in_range is False
    print("  PASS: Above range")


def test_supported_labs_loaded():
    """supported_labs.json is loaded and indexed correctly."""
    labs = _load_supported_labs()
    assert len(labs) == 27, f"Expected 27 supported labs, got {len(labs)}"
    assert "2345-7" in labs, "Glucose LOINC should be in supported labs"
    assert labs["2345-7"]["test_name"] == "Glucose"
    print(f"  PASS: supported_labs.json loaded ({len(labs)} tests)")


def test_medlineplus_resolve_loinc_from_test_name():
    """resolve_loinc_from_test_name works for known test names."""
    assert resolve_loinc_from_test_name("Glucose") == "2345-7"
    assert resolve_loinc_from_test_name("Hemoglobin") == "718-7"
    assert resolve_loinc_from_test_name("Creatinine") == "2160-0"
    assert resolve_loinc_from_test_name("Nonexistent Test") == ""
    print("  PASS: MedlinePlus LOINC resolution from test name")


def test_medlineplus_get_citation_no_fabrication():
    """get_citation never fabricates — returns 'no citation available' when needed."""
    citation = get_citation("", "NonexistentLab12345")
    assert "no MedlinePlus citation available" in citation
    # Should NOT say "MedlinePlus: NonexistentLab12345"
    assert citation == "NonexistentLab12345 — no MedlinePlus citation available"
    print("  PASS: No fabricated citations")


def test_medlineplus_get_citation_known_test():
    """get_citation returns something for a known test (may be fallback page)."""
    citation = get_citation("2345-7", "Glucose")
    # Should contain MedlinePlus or the test name
    assert "Glucose" in citation
    # Should NOT be fabricated
    assert citation != "MedlinePlus: Glucose"  # Should have more info than just the name
    print(f"  PASS: Citation for Glucose: {citation[:80]}...")


def test_batch_lookup_multiple_values():
    """Batch lookup handles multiple values correctly."""
    values = [
        ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=92.0, unit="mg/dL"),
        ExtractedLabValue(test_name="Creatinine", loinc_code="2160-0", value=0.9, unit="mg/dL"),
        ExtractedLabValue(test_name="Mystery", loinc_code="99999-9", value=1.0, unit="U"),
    ]
    result = lookup_ranges(values)
    assert len(result) == 3
    assert result[0].in_range is True   # Glucose 92 in 70-100
    assert result[1].in_range is True   # Creatinine 0.9 in 0.6-1.3
    assert result[2].in_range is False  # Unknown LOINC
    print("  PASS: Batch lookup handles multiple values")


if __name__ == "__main__":
    print("=== Reference Range Tests ===\n")
    test_known_loinc_glucose()
    test_known_loinc_hemoglobin()
    test_known_loinc_all_common_tests()
    test_unknown_loinc_cannot_become_normal()
    test_unknown_loinc_detail()
    test_missing_loinc_in_supported_but_no_range()
    test_unit_mismatch()
    test_boundary_values_lower()
    test_below_range()
    test_above_range()
    test_supported_labs_loaded()
    test_medlineplus_resolve_loinc_from_test_name()
    test_medlineplus_get_citation_no_fabrication()
    test_medlineplus_get_citation_known_test()
    test_batch_lookup_multiple_values()
    print("\nAll reference range tests passed.")
