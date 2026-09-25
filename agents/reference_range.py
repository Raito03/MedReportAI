"""Reference Range Lookup — SDK tool, NOT an LLM call.

Retrieves real reference ranges from the supplied reference_ranges.json
(documented MedlinePlus intervals). Never guessed by LLM.

Tool defined with input_schema/output_schema per spec.
"""

import json
from pathlib import Path
from typing import List, Optional

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

# Simple unit equivalences for common lab units
_UNIT_EQUIVALENCES = {
    # RBC count variations
    ("million cells/mcL", "M/uL"): True,
    ("M/uL", "million cells/mcL"): True,
    ("million/uL", "M/uL"): True,
    ("M/uL", "million/uL"): True,
    ("million cells/µL", "M/uL"): True,
    ("M/uL", "million cells/µL"): True,
    # WBC count variations
    ("cells/mcL", "K/uL"): False,  # different scale (thousands vs individual)
    ("K/uL", "cells/mcL"): False,
    # Platelet variations
    ("cells/mcL", "K/uL"): False,
    # Percentage variations
    ("%", "percent"): True,
    ("percent", "%"): True,
}


def _units_compatible(extracted_unit: str, reference_unit: str) -> bool:
    """Check if extracted unit is compatible with reference unit."""
    if extracted_unit.lower() == reference_unit.lower():
        return True
    key = (extracted_unit.strip(), reference_unit.strip())
    if key in _UNIT_EQUIVALENCES:
        return _UNIT_EQUIVALENCES[key]
    # Fallback: normalize and compare
    norm_extracted = extracted_unit.lower().replace("µ", "u").replace("×10^3", "K").replace("×10^6", "M")
    norm_ref = reference_unit.lower().replace("µ", "u").replace("×10^3", "K").replace("×10^6", "M")
    return norm_extracted == norm_ref


# --- Core Lookup Logic ---

def _lookup_single(params: RangeLookupInput) -> RangeCheckedOutput:
    """Look up one lab value against the reference range table.

    Handles:
    - Known LOINC with single range
    - Known LOINC with sex-specific ranges (requires population context)
    - Unknown LOINC -> controlled failure (NOT defaulting to normal)
    - Unit mismatch -> controlled failure
    """
    ranges = _load_ranges()
    supported = _load_supported_labs()

    # Check if LOINC is known in our reference ranges
    range_entries = ranges.get(params.loinc_code)

    if not range_entries:
        # LOINC not in reference data at all
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

    # Check unit compatibility with the first entry's canonical unit
    canonical_unit = range_entries[0].get("unit", "")
    if canonical_unit and not _units_compatible(params.unit, canonical_unit):
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

    # Filter by population context
    # If only one entry, use it (unisex range)
    if len(range_entries) == 1:
        entry = range_entries[0]
        ref_low = entry["reference_low"]
        ref_high = entry["reference_high"]
        in_range = ref_low <= params.value <= ref_high
        return RangeCheckedOutput(
            test_name=params.test_name,
            loinc_code=params.loinc_code,
            value=params.value,
            unit=params.unit,
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
        in_range = ref_low <= params.value <= ref_high
        return RangeCheckedOutput(
            test_name=params.test_name,
            loinc_code=params.loinc_code,
            value=params.value,
            unit=params.unit,
            reference_low=ref_low,
            reference_high=ref_high,
            in_range=in_range,
            range_available=True,
            range_note="",
        )

    # Sex-specific ranges exist but no population context provided
    # Return a controlled "insufficient context" result — do NOT guess
    sex_options = [e.get("population", {}).get("sex", "unknown") for e in range_entries]
    return RangeCheckedOutput(
        test_name=params.test_name,
        loinc_code=params.loinc_code,
        value=params.value,
        unit=params.unit,
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
    """Batch lookup — called by orchestrator for direct (non-LLM) calls."""
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
            reference_low=r.reference_low if r.range_available else 0.0,
            reference_high=r.reference_high if r.range_available else 0.0,
            in_range=r.in_range,
        )
        for r in output.results
    ]


def lookup_range_detail(params: RangeLookupInput) -> RangeCheckedOutput:
    """Detailed single lookup — returns full metadata including range availability and notes."""
    return _lookup_single(params)
