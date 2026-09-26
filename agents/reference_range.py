"""Reference Range Lookup — SDK tool, NOT an LLM call.

Retrieves real reference ranges from the supplied reference_ranges.json
(documented MedlinePlus intervals). Never guessed by LLM.

Tool defined with input_schema/output_schema per spec.
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel
from openrouter_agent import tool

from core.schemas import ExtractedLabValue, RangeCheckedValue

LOOKUP_PATH = Path(__file__).resolve().parent.parent / "data" / "reference_ranges.json"
SUPPORTED_LABS_PATH = Path(__file__).resolve().parent.parent / "data" / "supported_labs.json"


# --- SDK Tool Schemas ---

class RangeLookupInput(BaseModel):
    """Input: a single lab value to look up."""
    loinc_code: str
    test_name: str
    value: float
    unit: str


class RangeCheckedOutput(BaseModel):
    """Output: the lab value with reference range info attached."""
    test_name: str
    loinc_code: str
    value: float
    unit: str
    reference_low: Optional[float] = None
    reference_high: Optional[float] = None
    in_range: bool = False
    range_available: bool = False
    range_note: str = ""


class BatchRangeLookupInput(BaseModel):
    """Input: multiple lab values to look up at once."""
    values: List[RangeLookupInput]


class BatchRangeLookupOutput(BaseModel):
    """Output: all lab values with reference ranges attached."""
    results: List[RangeCheckedOutput]


# --- Data Loading ---

_ranges_data: dict = None
_supported_labs: dict = None


def _load_ranges() -> dict:
    """Load the reference range lookup table (cached)."""
    global _ranges_data
    if _ranges_data is None:
        with open(LOOKUP_PATH, "r") as f:
            raw = json.load(f)
        _ranges_data = raw.get("ranges_by_loinc", {})
    return _ranges_data


def _load_supported_labs() -> dict:
    """Load supported labs metadata (cached)."""
    global _supported_labs
    if _supported_labs is None:
        with open(SUPPORTED_LABS_PATH, "r") as f:
            raw = json.load(f)
        # Index by LOINC code for fast lookup
        _supported_labs = {}
        for test in raw.get("tests", []):
            _supported_labs[test["loinc_code"]] = test
    return _supported_labs


# --- Unit Handling ---

# Conversion factors: (source_unit, target_unit) -> factor to multiply source value by
# These are TRUE numerical conversions, not aliases.
_UNIT_CONVERSIONS = {
    # WBC: K/uL (thousands) -> cells/mcL (individual)
    # 7 K/uL = 7000 cells/mcL
    ("K/uL", "cells/mcL"): 1000.0,
    ("K/uL", "cells/µL"): 1000.0,
    ("10^3/uL", "cells/mcL"): 1000.0,
    ("10^3/uL", "cells/µL"): 1000.0,
    # Reverse: cells/mcL -> K/uL
    ("cells/mcL", "K/uL"): 0.001,
    ("cells/µL", "K/uL"): 0.001,
    ("cells/mcL", "10^3/uL"): 0.001,
    ("cells/µL", "10^3/uL"): 0.001,
    # RBC: M/uL (millions) -> million cells/mcL (same scale, notation alias)
    # These are numerically equivalent — factor 1.0
    ("M/uL", "million cells/mcL"): 1.0,
    ("M/uL", "million cells/µL"): 1.0,
    ("M/uL", "million/uL"): 1.0,
    ("M/uL", "10^6/uL"): 1.0,
    ("million cells/mcL", "M/uL"): 1.0,
    ("million cells/µL", "M/uL"): 1.0,
    ("million/uL", "M/uL"): 1.0,
    ("10^6/uL", "M/uL"): 1.0,
}

# Pure text aliases (no numerical change, just formatting normalization)
_UNIT_ALIASES = {
    "%": "%",
    "percent": "%",
    "pg/cell": "pg",
    "pg": "pg/cell",
}


def _normalize_unit(unit: str) -> str:
    """Normalize unit string for comparison (lowercase, strip whitespace, unify µ/µ)."""
    return unit.strip().lower().replace("µ", "u").replace("×10^3", "10^3").replace("×10^6", "10^6")


def _get_conversion_factor(source_unit: str, target_unit: str) -> Optional[float]:
    """Get the numerical conversion factor from source to target unit.

    Returns the factor to multiply source value by, or None if conversion
    is not supported.
    """
    norm_source = _normalize_unit(source_unit)
    norm_target = _normalize_unit(target_unit)

    # Check exact normalized match (same unit)
    if norm_source == norm_target:
        return 1.0

    # Check aliases
    alias_source = _UNIT_ALIASES.get(norm_source, norm_source)
    alias_target = _UNIT_ALIASES.get(norm_target, norm_target)
    if alias_source == alias_target:
        return 1.0

    # Check conversion factors (try normalized and original forms)
    for key, factor in _UNIT_CONVERSIONS.items():
        if _normalize_unit(key[0]) == norm_source and _normalize_unit(key[1]) == norm_target:
            return factor
        if key[0] == source_unit and key[1] == target_unit:
            return factor

    return None


def _convert_value(value: float, source_unit: str, target_unit: str) -> Tuple[float, str, bool]:
    """Convert a value from source unit to target unit.

    Returns (converted_value, canonical_unit, success).
    If conversion is not supported, returns (original_value, original_unit, False).
    """
    factor = _get_conversion_factor(source_unit, target_unit)
    if factor is None:
        return value, source_unit, False
    return value * factor, target_unit, True


# --- Core Lookup Logic ---

def _lookup_single(params: RangeLookupInput) -> RangeCheckedOutput:
    """Look up one lab value against the reference range table.

    Handles:
    - Known LOINC with single range
    - Known LOINC with sex-specific ranges (requires population context)
    - Unknown LOINC -> controlled failure (NOT defaulting to normal)
    - Unit mismatch -> controlled failure
    - Unit conversion (e.g. K/uL -> cells/mcL)
    """
    ranges = _load_ranges()
    supported = _load_supported_labs()

    # Check if LOINC is known in our reference ranges
    range_entries = ranges.get(params.loinc_code)

    if not range_entries:
        # Case A: Unknown LOINC
        is_supported = params.loinc_code in supported
        if is_supported:
            note = f"LOINC {params.loinc_code} is recognized but no reference range is available"
        else:
            note = f"LOINC {params.loinc_code} is not in the supported labs list"
        return RangeCheckedOutput(
            test_name=params.test_name,
            loinc_code=params.loinc_code,
            value=params.value,
            unit=params.unit,
            reference_low=None,
            reference_high=None,
            in_range=False,
            range_available=False,
            range_note=note,
        )

    # Check unit compatibility and attempt conversion
    canonical_unit = range_entries[0].get("unit", "")
    converted_value = params.value
    used_unit = params.unit

    if canonical_unit:
        factor = _get_conversion_factor(params.unit, canonical_unit)
        if factor is None:
            # Case C: incompatible unit — controlled failure
            return RangeCheckedOutput(
                test_name=params.test_name,
                loinc_code=params.loinc_code,
                value=params.value,
                unit=params.unit,
                reference_low=None,
                reference_high=None,
                in_range=False,
                range_available=False,
                range_note=f"Unit mismatch: extracted '{params.unit}' vs reference '{canonical_unit}'",
            )
        elif factor != 1.0:
            # Perform numerical conversion
            converted_value = params.value * factor
            used_unit = canonical_unit

    # Filter by population context
    # If only one entry, use it (unisex range)
    if len(range_entries) == 1:
        entry = range_entries[0]
        ref_low = entry["reference_low"]
        ref_high = entry["reference_high"]
        in_range = ref_low <= converted_value <= ref_high
        return RangeCheckedOutput(
            test_name=params.test_name,
            loinc_code=params.loinc_code,
            value=converted_value,
            unit=used_unit,
            reference_low=ref_low,
            reference_high=ref_high,
            in_range=in_range,
            range_available=True,
            range_note="",
        )

    # Multiple entries — likely sex-specific
    # Try to find an "all" or unisex entry first
    all_entries = [e for e in range_entries if e.get("population", {}).get("sex") == "all"]
    if len(all_entries) == 1:
        entry = all_entries[0]
        ref_low = entry["reference_low"]
        ref_high = entry["reference_high"]
        in_range = ref_low <= converted_value <= ref_high
        return RangeCheckedOutput(
            test_name=params.test_name,
            loinc_code=params.loinc_code,
            value=converted_value,
            unit=used_unit,
            reference_low=ref_low,
            reference_high=ref_high,
            in_range=in_range,
            range_available=True,
            range_note="",
        )

    # Case B: Sex-specific ranges exist but no population context provided
    # Return a controlled "insufficient context" result — do NOT guess
    sex_options = [e.get("population", {}).get("sex", "unknown") for e in range_entries]
    return RangeCheckedOutput(
        test_name=params.test_name,
        loinc_code=params.loinc_code,
        value=converted_value,
        unit=used_unit,
        reference_low=None,
        reference_high=None,
        in_range=False,
        range_available=False,
        range_note=f"Sex-specific ranges available ({', '.join(sex_options)}) but patient sex not provided. Cannot determine which range to apply.",
    )


def _execute_batch_lookup(params: BatchRangeLookupInput) -> BatchRangeLookupOutput:
    """Batch lookup — the tool's execute handler."""
    results = [_lookup_single(v) for v in params.values]
    return BatchRangeLookupOutput(results=results)


range_lookup_tool = tool(
    name="lookup_reference_range",
    description=(
        "Look up the real reference range for a lab test by LOINC code. "
        "Returns reference_low, reference_high, and whether the value is in range. "
        "NEVER guesses ranges — always uses the local MedlinePlus/LOINC lookup table. "
        "Returns controlled failure for unknown LOINCs, unit mismatches, or missing population context."
    ),
    input_schema=BatchRangeLookupInput,
    output_schema=BatchRangeLookupOutput,
    execute=_execute_batch_lookup,
)


# --- Batch Lookup (used by orchestrator directly) ---

def lookup_ranges(values: List[ExtractedLabValue]) -> List[RangeCheckedValue]:
    """Batch lookup — called by orchestrator for direct (non-LLM) calls.

    IMPORTANT: Preserves the unavailable-reference state (None for ref_low/ref_high)
    instead of converting to 0.0. Downstream agents must check range_available
    before interpreting reference_low/reference_high.
    """
    params = BatchRangeLookupInput(
        values=[
            RangeLookupInput(
                loinc_code=v.loinc_code,
                test_name=v.test_name,
                value=v.value,
                unit=v.unit,
            )
            for v in values
        ]
    )
    output = _execute_batch_lookup(params)
    return [
        RangeCheckedValue(
            test_name=r.test_name,
            loinc_code=r.loinc_code,
            value=r.value,
            unit=r.unit,
            reference_low=r.reference_low,
            reference_high=r.reference_high,
            in_range=r.in_range,
            range_available=r.range_available,
            range_note=r.range_note,
        )
        for r in output.results
    ]


def lookup_range_detail(params: RangeLookupInput) -> RangeCheckedOutput:
    """Detailed single lookup — returns full metadata including range availability and notes."""
    return _lookup_single(params)
