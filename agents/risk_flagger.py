"""Risk Flagging Agent — classifies values as normal / mildly_abnormal / critical.

LLM call #2, grounded by the lookup result.
Tool defined with input_schema/output_schema per spec.

Safety: Values with no valid reference range are NOT classified from range.
They are passed through with status="unavailable" so downstream agents
know the system cannot safely assess them.
"""

import json
import re
from typing import List

from pydantic import BaseModel
from openrouter_agent import call_model, step_count_is, tool

from core.config import OPENROUTER_MODEL
from core.llm_client import get_client
from core.schemas import RangeCheckedValue, RiskFlaggedValue


# --- SDK Tool Schemas ---

class RiskClassificationInput(BaseModel):
    """Input: range-checked lab values to classify."""
    values: List[RangeCheckedValue]


class RiskClassificationOutput(BaseModel):
    """Output: lab values with risk status assigned."""
    classifications: List[RiskFlaggedValue]


# --- SDK Tool Definition ---

def _execute_risk_classification(params: RiskClassificationInput) -> RiskClassificationOutput:
    """Stub — actual execution happens in async classify_risk()."""
    return RiskClassificationOutput(classifications=[])


risk_classification_tool = tool(
    name="classify_lab_risk",
    description=(
        "Classify lab values as normal, mildly_abnormal, or critical "
        "based on how far they deviate from reference ranges. "
        "Does NOT diagnose — only measures deviation from range."
    ),
    input_schema=RiskClassificationInput,
    output_schema=RiskClassificationOutput,
    execute=_execute_risk_classification,
)


# --- Prompt ---

SYSTEM_PROMPT = """\
You are a clinical risk classification engine. You receive lab values WITH their reference ranges already looked up.

For EACH value, compare the "value" field against "reference_low" and "reference_high":
- If reference_low <= value <= reference_high: status = "normal"
- If value is slightly outside (within 20% of boundary): status = "mildly_abnormal"
- If value is far outside (more than 20% beyond boundary): status = "critical"

Example: Glucose value=92, reference_low=70, reference_high=100 → 92 is BETWEEN 70 and 100 → status="normal"

Return ONLY a JSON array. No markdown fences, no explanation, just the JSON.

For each value, use these exact keys:
- test_name
- loinc_code (copy from input exactly)
- value (number)
- unit
- status: one of "normal", "mildly_abnormal", "critical"
- reasoning: one sentence explaining the classification

IMPORTANT: Do NOT diagnose. Only classify deviation from range.
IMPORTANT: Always include loinc_code exactly as provided in the input.
IMPORTANT: Read the reference_low and reference_high carefully. If the value is between them, it IS normal."""


def _extract_json(text: str):
    """Robustly extract JSON from LLM response."""
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

async def classify_risk(checked: List[RangeCheckedValue]) -> List[RiskFlaggedValue]:
    """Classify risk level for each range-checked lab value.

    SAFETY: Values with range_available=False are NOT sent to the LLM
    for range-based classification. They are returned with
    status="unavailable" and a reasoning explaining why.
    """
    # Separate values with available vs unavailable reference ranges
    classifiable = []
    unclassifiable = []

    for c in checked:
        if c.range_available and c.reference_low is not None and c.reference_high is not None:
            classifiable.append(c)
        else:
            unclassifiable.append(c)

    # Pre-populate results for unclassifiable values — NO range-based classification
    results: List[RiskFlaggedValue] = []
    for c in unclassifiable:
        note = c.range_note if hasattr(c, 'range_note') and c.range_note else "No valid reference range available"
        results.append(RiskFlaggedValue(
            test_name=c.test_name,
            loinc_code=c.loinc_code if hasattr(c, 'loinc_code') else "",
            value=c.value,
            unit=c.unit,
            status="unavailable",
            reasoning=(
                f"Cannot classify: {note.rstrip('. ')}; no valid applicable "
                "reference range was available, so no range-based risk "
                "classification was performed."
            ),
        ))

    # Classify values with valid reference ranges via LLM
    if classifiable:
        client = get_client()
        context = json.dumps([c.model_dump() for c in classifiable], indent=2)
        combined = f"{SYSTEM_PROMPT}\n\nUSER INPUT:\nClassify these lab values:\n{context}"

        result = call_model(
            client,
            {
                "model": OPENROUTER_MODEL,
                "input": combined,
                "tools": [risk_classification_tool],
                "stop_when": step_count_is(2),
            },
        )

        text = await result.get_text()
        data = _extract_json(text)

        if isinstance(data, dict):
            data = data.get("classifications", data.get("values", []))

        # SAFETY (P0-T2): only values that were actually sent for range-based
        # classification may receive an LLM-assigned status. Any item the model
        # invents for an unavailable-range value (or an unknown test) is dropped
        # so range_available=False can never become normal/mildly_abnormal/critical.
        classifiable_names = {c.test_name for c in classifiable}

        for item in data:
            if not isinstance(item, dict) or item.get("test_name", "") not in classifiable_names:
                continue
            # Ensure loinc_code is threaded from input
            if "loinc_code" not in item or not item["loinc_code"]:
                # Find matching checked value to get loinc_code
                for c in classifiable:
                    if c.test_name == item.get("test_name", ""):
                        item["loinc_code"] = c.loinc_code
                        break
            results.append(RiskFlaggedValue(**item))

    return results
