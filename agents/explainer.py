"""Explanation Agent — plain-language explanation + doctor questions + citation.

LLM call #3.
Tool defined with input_schema/output_schema per spec.
"""

import json
import re
from typing import List

from pydantic import BaseModel
from openrouter_agent import call_model, step_count_is, tool

from core.config import OPENROUTER_MODEL
from core.llm_client import get_client
from core.schemas import RiskFlaggedValue, FinalExplanation


# --- SDK Tool Schemas ---

class ExplanationInput(BaseModel):
    """Input: risk-classified lab values to explain."""
    values: List[RiskFlaggedValue]


class ExplanationOutput(BaseModel):
    """Output: plain-language explanations for each lab value."""
    explanations: List[FinalExplanation]


# --- SDK Tool Definition ---

def _execute_explanation(params: ExplanationInput) -> ExplanationOutput:
    """Stub — actual execution happens in async explain()."""
    return ExplanationOutput(explanations=[])


explanation_tool = tool(
    name="explain_lab_results",
    description=(
        "Generate patient-friendly plain-language explanations for lab results. "
        "Each explanation includes a source citation and suggested doctor questions. "
        "Never diagnoses — uses hedging language like 'results in this range are generally considered...'"
    ),
    input_schema=ExplanationInput,
    output_schema=ExplanationOutput,
    execute=_execute_explanation,
)


# --- Prompt ---

SYSTEM_PROMPT = """\
You are a patient-friendly medical explainer. Given classified lab values, \
write a plain-language explanation for each.

Return ONLY a JSON array. No markdown fences, no explanation, just the JSON.

For each value, use these exact keys:
- test_name
- explanation: 1-2 sentences in plain English. NO diagnostic claims. \
NO "you have..." or "this indicates..." phrasing. Use hedging: \
"results in this range are generally considered..."
- doctor_questions: array of 2-3 strings
- citation: source reference (e.g. "MedlinePlus: [topic]")

CRITICAL: Never state or imply a diagnosis. Always include a citation."""


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

async def explain(risk_flagged: List[RiskFlaggedValue]) -> List[FinalExplanation]:
    """Generate plain-language explanations for each risk-flagged value."""
    client = get_client()
    context = json.dumps([r.model_dump() for r in risk_flagged], indent=2)
    combined = f"{SYSTEM_PROMPT}\n\nUSER INPUT:\nExplain these lab results:\n{context}"

    result = call_model(
        client,
        {
            "model": OPENROUTER_MODEL,
            "input": combined,
            "tools": [explanation_tool],
            "stop_when": step_count_is(2),
        },
    )

    text = await result.get_text()
    data = _extract_json(text)

    if isinstance(data, dict):
        data = data.get("explanations", data.get("values", []))

    output = ExplanationOutput(
        explanations=[FinalExplanation(**item) for item in data]
    )
    return output.explanations
