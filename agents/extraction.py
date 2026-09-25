"""Extraction Agent — raw text → List[ExtractedLabValue].

LLM call #1. Uses call_model() from openrouter-agent-sdk.
Tool defined with input_schema/output_schema per spec.
Post-processes: resolves LOINC codes from test names via supported_labs.json.
"""

import json
import re
from typing import List

from pydantic import BaseModel
from openrouter_agent import call_model, step_count_is, tool

from core.config import OPENROUTER_MODEL
from core.llm_client import get_client
from core.schemas import ExtractedLabValue
from tools.medlineplus_connect import resolve_loinc_from_test_name


# --- SDK Tool Schemas ---

class ExtractionInput(BaseModel):
    """Input: raw text extracted from a lab report PDF."""
    raw_text: str


class ExtractionOutput(BaseModel):
    """Output: list of structured lab values extracted from the text."""
    values: List[ExtractedLabValue]


# --- SDK Tool Definition ---

def _execute_extraction(params: ExtractionInput) -> ExtractionOutput:
    """Synchronous stub — actual execution happens in async extract_lab_values()."""
    return ExtractionOutput(values=[])


extraction_tool = tool(
    name="extract_lab_values",
    description=(
        "Extract structured lab values from raw PDF text. "
        "Returns test name, LOINC code, numeric value, and unit for each test found."
    ),
    input_schema=ExtractionInput,
    output_schema=ExtractionOutput,
    execute=_execute_extraction,
)


# --- Prompt ---

SYSTEM_PROMPT = """\
You are a lab report extraction engine. Given raw text from a blood test PDF,
extract every lab value you find.

Return ONLY a JSON array. No markdown fences, no explanation, just the JSON.

For each value, use these exact keys:
- test_name: the name of the test (e.g. "Glucose", "Hemoglobin")
- loinc_code: the LOINC code if present in the text; if not, use "unknown"
- value: the numeric result (number, not string)
- unit: the unit of measurement

Example:
[{"test_name":"Glucose","loinc_code":"2345-7","value":95,"unit":"mg/dL"}]"""


# --- JSON Parsing ---

def _extract_json(text: str):
    """Robustly extract JSON from LLM response, handling code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    for start_char, end_char in [('[', ']'), ('{', '}')]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")


# --- Async Entry Point ---

async def extract_lab_values(raw_text: str) -> List[ExtractedLabValue]:
    """Extract structured lab values from raw PDF text via LLM.

    Post-processes: resolves LOINC codes from test names using supported_labs.json
    when the LLM returns "unknown" for a LOINC code.
    """
    client = get_client()
    combined = f"{SYSTEM_PROMPT}\n\nUSER INPUT:\n{raw_text}"

    result = call_model(
        client,
        {
            "model": OPENROUTER_MODEL,
            "input": combined,
            "tools": [extraction_tool],
            "stop_when": step_count_is(2),
        },
    )

    text = await result.get_text()
    data = _extract_json(text)

    if isinstance(data, dict):
        data = data.get("values", data.get("lab_values", []))

    # Validate each item through the output schema
    values = [ExtractedLabValue(**item) for item in data]

    # Post-process: resolve LOINC codes from test names when "unknown"
    resolved = []
    for v in values:
        if v.loinc_code == "unknown" or not v.loinc_code:
            resolved_loinc = resolve_loinc_from_test_name(v.test_name)
            if resolved_loinc:
                v = v.model_copy(update={"loinc_code": resolved_loinc})
        resolved.append(v)

    return resolved
