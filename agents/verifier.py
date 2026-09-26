"""Verifier Agent — safety check + self-correction trigger.

Hybrid code+LLM check.
Tool defined with input_schema/output_schema per spec.
"""

import json
import re
from typing import List

from pydantic import BaseModel
from openrouter_agent import call_model, step_count_is, tool

from core.config import OPENROUTER_MODEL
from core.llm_client import get_client
from core.schemas import FinalExplanation, RiskFlaggedValue, VerifierResult


# --- Diagnostic Language Blocklist ---

DIAGNOSTIC_PATTERNS = [
    r"\byou have\b",
    r"\byou are\b.*(?:diabetic|anemic|hypothyroid|hyperthyroid)",
    r"\bthis means you\b",
    r"\bthis shows you\b",
    r"\byou have been diagnosed\b",
    r"\bdiagnos(?:is|ed|e|ing)\b",
    r"\byou have a (?:disease|condition|illness)\b",
]


# --- SDK Tool Schemas ---

class VerificationInput(BaseModel):
    """Input: explanations and risk classifications to verify."""
    explanations: List[FinalExplanation]
    risk_classifications: List[RiskFlaggedValue]


class VerificationOutput(BaseModel):
    """Output: verification result with pass/fail and issues found."""
    passed: bool
    issues_found: List[str]
    action: str  # "approve" | "send_back_for_correction"


# --- SDK Tool Definition ---

def _execute_verification(params: VerificationInput) -> VerificationOutput:
    """Stub — actual execution happens in async verify()."""
    return VerificationOutput(passed=True, issues_found=[], action="approve")


verification_tool = tool(
    name="verify_explanations",
    description=(
        "Safety-check lab report explanations for diagnostic language, "
        "missing citations, and factual accuracy. Returns approve or send_back_for_correction."
    ),
    input_schema=VerificationInput,
    output_schema=VerificationOutput,
    execute=_execute_verification,
)


# --- Prompt ---

SYSTEM_PROMPT = """\
You are a safety verifier for medical lab report explanations.

ONLY flag these specific problems:
1. Direct diagnostic claims: "you have [condition]", "you are diabetic/anemic", "this means you have"
2. Missing citations
3. Alarming or fear-inducing language

DO NOT flag these (they are SAFE hedging language, NOT diagnostic):
- "generally considered" / "typically viewed as" / "usually regarded as"
- "results in this range" / "values in this range"
- "reflects" / "suggests" / "indicates" when used to describe what a test measures
- Any language that explains what a test measures in general terms

Return ONLY a JSON object. No markdown fences, no explanation, just the JSON.

Keys:
- passed: boolean
- issues_found: array of strings describing each problem found"""


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
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")


def _code_check(explanations: List[FinalExplanation]) -> List[str]:
    """Fast, deterministic regex checks."""
    issues = []
    for exp in explanations:
        for pattern in DIAGNOSTIC_PATTERNS:
            if re.search(pattern, exp.explanation, re.IGNORECASE):
                issues.append(
                    f"[{exp.test_name}] Contains diagnostic language: "
                    f"matches pattern '{pattern}'"
                )
        if not exp.citation or exp.citation.strip() == "":
            issues.append(f"[{exp.test_name}] Missing citation")
    return issues


# --- Async Entry Point ---

async def verify(
    explanations: List[FinalExplanation],
    risk_flagged: List[RiskFlaggedValue],
) -> VerifierResult:
    """Verify explanations for safety before showing to user."""
    # Code-based checks first (fast, deterministic)
    issues = _code_check(explanations)

    # LLM-based review for nuance
    client = get_client()
    expl_data = json.dumps([e.model_dump() for e in explanations], indent=2)
    risk_data = json.dumps([r.model_dump() for r in risk_flagged], indent=2)
    combined = (
        f"{SYSTEM_PROMPT}\n\n"
        f"USER INPUT:\nExplanations:\n{expl_data}\n\n"
        f"Risk classifications:\n{risk_data}"
    )

    result = call_model(
        client,
        {
            "model": OPENROUTER_MODEL,
            "input": combined,
            "tools": [verification_tool],
            "stop_when": step_count_is(2),
        },
    )

    text = await result.get_text()
    llm_result = _extract_json(text)

    # P0-T4 seam guard: fail closed on malformed LLM output. Contract is
    # {"passed": bool, "issues_found": list[str]} — a missing field, wrong
    # type, or self-contradictory response must NEVER be silently treated
    # as valid approval.
    if (
        not isinstance(llm_result, dict)
        or not isinstance(llm_result.get("passed"), bool)
        or not isinstance(llm_result.get("issues_found"), list)
        or not all(isinstance(i, str) for i in llm_result["issues_found"])
    ):
        llm_issues = [
            "Verifier LLM returned malformed output "
            "(expected passed: bool, issues_found: list[str]) "
            "— failing closed for review"
        ]
    else:
        llm_issues = list(llm_result["issues_found"])
        if llm_result["passed"] is False and not llm_issues:
            llm_issues = [
                "Verifier LLM reported passed=false without issue details "
                "— failing closed for review"
            ]

    all_issues = issues + llm_issues
    passed = len(all_issues) == 0

    output = VerificationOutput(
        passed=passed,
        issues_found=all_issues,
        action="approve" if passed else "send_back_for_correction",
    )
    return VerifierResult(
        passed=output.passed,
        issues_found=output.issues_found,
        action=output.action,
    )
