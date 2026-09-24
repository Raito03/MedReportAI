"""Reference range lookup correctness tests."""

from agents.reference_range import lookup_ranges
from core.schemas import ExtractedLabValue


def test_known_loinc_code():
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=95.0, unit="mg/dL")]
    result = lookup_ranges(values)
    assert len(result) == 1
    assert result[0].reference_low == 70.0
    assert result[0].reference_high == 100.0
    assert result[0].in_range is True


def test_out_of_range():
    values = [ExtractedLabValue(test_name="Glucose", loinc_code="2345-7", value=200.0, unit="mg/dL")]
    result = lookup_ranges(values)
    assert result[0].in_range is False


def test_unknown_loinc():
    values = [ExtractedLabValue(test_name="Mystery Test", loinc_code="99999-9", value=42.0, unit="U")]
    result = lookup_ranges(values)
    assert len(result) == 1
    # Should get default 0/0 range
    assert result[0].reference_low == 0.0


if __name__ == "__main__":
    test_known_loinc_code()
    test_out_of_range()
    test_unknown_loinc()
    print("All reference range tests passed.")
