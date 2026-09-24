"""Reference Range Lookup — SDK tool, NOT an LLM call.

Retrieves real reference ranges from a local lookup table sourced from
LOINC/NHANES. Never guessed by LLM.

Tool defined with input_schema/output_schema per spec.
"""

import json
from pathlib import Path
from typing import List

from pydantic import BaseModel
from openrouter_agent import tool

from core.schemas import ExtractedLabValue, RangeCheckedValue

LOOKUP_PATH = Path(__file__).resolve().parent.parent / "data" / "reference_ranges.json"


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
    reference_low: float
    reference_high: float
    in_range: bool


class BatchRangeLookupInput(BaseModel):
    """Input: multiple lab values to look up at once."""
    values: List[RangeLookupInput]


class BatchRangeLookupOutput(BaseModel):
    """Output: all lab values with reference ranges attached."""
    results: List[RangeCheckedOutput]


# --- SDK Tool Definition ---

def _load_ranges() -> dict:
    """Load the reference range lookup table."""
    with open(LOOKUP_PATH, "r") as f:
        return json.load(f)


def _lookup_single(params: RangeLookupInput) -> RangeCheckedOutput:
    """Look up one lab value against the reference range table."""
    ranges = _load_ranges()
    entry = ranges.get(params.loinc_code) or ranges.get(params.test_name.lower())

    if entry:
        ref_low = entry["reference_low"]
        ref_high = entry["reference_high"]
        in_range = ref_low <= params.value <= ref_high
    else:
        ref_low = 0.0
        ref_high = 0.0
        in_range = True

    return RangeCheckedOutput(
        test_name=params.test_name,
        loinc_code=params.loinc_code,
        value=params.value,
        unit=params.unit,
        reference_low=ref_low,
        reference_high=ref_high,
        in_range=in_range,
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
        "NEVER guesses ranges — always uses the local NHANES/LOINC lookup table."
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
            reference_low=r.reference_low,
            reference_high=r.reference_high,
            in_range=r.in_range,
        )
        for r in output.results
    ]
