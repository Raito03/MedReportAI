"""Explanation Agent — plain-language explanation + doctor questions + citation.

LLM call #3.
Tool defined with input_schema/output_schema per spec.

Uses MedlinePlus Connect for real citations (not fabricated).
"""

import json
import re
from typing import List, Optional

from pydantic import BaseModel
from openrouter_agent import call_model, step_count_is, tool

from core.config import OPENROUTER_MODEL
from core.llm_client import get_client
from core.schemas import RiskFlaggedValue, FinalExplanation
from tools.medlineplus_connect import (
    CITATION_UNAVAILABLE_MARKER,
    derive_citation_fields,
    get_citation,
)


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
- test_name: copy it exactly from the input
- explanation: 1-2 sentences in plain English. NO diagnostic claims. \
NO "you have..." or "this indicates..." phrasing. Use hedging: \
"results in this range are generally considered..."
- doctor_questions: array of 2-3 strings
- citation: USE THE CITATION PROVIDED IN THE INPUT — do not make up citations \
and do not change or invent any URL in it. If the input's citation_status is \
"unavailable", repeat the provided no-citation text as-is.

CRITICAL: Never state or imply a diagnosis. Always use the citation provided. \
If no citation is available in the input, do not fabricate one."""


# --- P1-T2 bounded self-correction -----------------------------------------
# Appended to the prompt ONLY when the verifier has rejected the previous
# draft and the orchestrator is retrying (bounded by MAX_RETRIES). The
# verifier's issues are passed through verbatim; the rules below restate —
# never relax — the safety constraints, so a correction cannot weaken
# verification, drop citation requirements, change lab values, or alter a
# risk classification just to make verification pass.
CORRECTION_PROMPT_TEMPLATE = """\
CORRECTION REQUESTED — the safety verifier REJECTED your previous draft.
It found these issues:
{issues}

Fix every issue listed above. These rules do NOT change:
- Never state or imply a diagnosis (no "you have ..." claims).
- Do not change any test_name, value, unit, or risk classification.
- Use ONLY the citations provided in the input — never invent or modify one.
- Keep the same JSON array output format.
If a citation_status is "unavailable", repeat the provided no-citation text as-is."""



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

async def explain(
    risk_flagged: List[RiskFlaggedValue],
    correction_issues: Optional[List[str]] = None,
) -> List[FinalExplanation]:
    """Generate plain-language explanations for each risk-flagged value.

    Each explanation gets a real MedlinePlus citation (not fabricated).

    P1-T2 self-correction: when ``correction_issues`` (the verifier's
    ``issues_found`` from a rejected draft) is provided, those issues are
    injected verbatim into the prompt as a bounded correction request.
    Without it the prompt is byte-identical to the initial-generation prompt.
    Citation grounding, post-processing, and all safety rules are unchanged.
    """
    # P0-T4 seam guard: empty input short-circuits — never call the LLM with
    # zero values (a model asked to explain nothing could invent results for
    # values that do not exist).
    if not risk_flagged:
        return []
    client = get_client()

    # Enrich risk_flagged values with real citations from MedlinePlus Connect.
    # P0-T6: build the trusted citation map keyed by test_name — this map is
    # the ONLY source of citations in the final output; the LLM cannot
    # introduce or modify one.
    enriched = []
    trusted_citations = {}
    for r in risk_flagged:
        test_name = r.test_name if hasattr(r, "test_name") else r.get("test_name", "")
        loinc = r.loinc_code if hasattr(r, "loinc_code") else r.get("loinc_code", "")
        citation = get_citation(loinc, test_name)
        if test_name not in trusted_citations:
            trusted_citations[test_name] = citation
        citation_url, citation_status = derive_citation_fields(citation)
        r_dict = r.model_dump() if hasattr(r, "model_dump") else r
        enriched.append({
            **r_dict,
            "citation": citation,
            "citation_url": citation_url,
            "citation_status": citation_status,
        })

    context = json.dumps(enriched, indent=2)
    combined = f"{SYSTEM_PROMPT}\n\nUSER INPUT:\nExplain these lab results:\n{context}"
    if correction_issues:
        # P1-T2: the verifier's findings drive this regeneration. Issues are
        # passed verbatim; only the prompt gains a correction section — the
        # risk data above and the trusted citation map stay exactly as-is.
        issues_text = "\n".join(f"- {issue}" for issue in correction_issues)
        combined += "\n\n" + CORRECTION_PROMPT_TEMPLATE.format(issues=issues_text)

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

    # Post-process (P0-T6): citations are ALWAYS taken from the trusted
    # lookup map — whatever the LLM returned is discarded. A test_name the
    # LLM invented (not in the input) gets the explicit no-citation state,
    # never a made-up source.
    explanations = []
    for item in data:
        # Citation grounding fields are ours, not the model's: strip any
        # values the LLM echoed/invented before validating the schema.
        if isinstance(item, dict):
            item.pop("citation_url", None)
            item.pop("citation_status", None)
        exp = FinalExplanation(**item)
        trusted = trusted_citations.get(exp.test_name)
        if trusted is None:
            trusted = f"{exp.test_name} — {CITATION_UNAVAILABLE_MARKER}"
        exp.citation = trusted
        exp.citation_url, exp.citation_status = derive_citation_fields(trusted)
        explanations.append(exp)

    return explanations

