"""Risk Flagging Agent — classifies values as normal / mildly_abnormal / critical.

LLM call #2, grounded by the lookup result.
Tool defined with input_schema/output_schema per spec.
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
You are a clinical risk classification engine. Given lab values with their \
reference ranges, classify each as:
- "normal": value is within reference range
- "mildly_abnormal": value is slightly outside range (within 20% of boundary)
- "critical": value is far outside range (more than 20% beyond boundary)

Return ONLY a JSON array. No markdown fences, no explanation, just the JSON.

For each value, use these exact keys:
- test_name
- value (number)
- unit
- status: one of "normal", "mildly_abnormal", "critical"
- reasoning: one sentence explaining the classification

IMPORTANT: Do NOT diagnose. Only classify deviation from range."""


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
    """Classify risk level for each range-checked lab value."""
    client = get_client()
    context = json.dumps([c.model_dump() for c in checked], indent=2)
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

    output = RiskClassificationOutput(
        classifications=[RiskFlaggedValue(**item) for item in data]
    )
    return output.classifications
