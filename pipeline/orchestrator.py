"""Orchestrator — wires all agents sequentially with error handling.

Flow: PDF → extract → lookup → risk → explain → verify
If verifier returns send_back_for_correction, retry once with issues injected.
All agent calls are async (openrouter-agent-sdk uses call_model()).
"""

import asyncio
from typing import List
from tools.pdf_extractor import pdf_to_text
from agents.extraction import extract_lab_values
from agents.reference_range import lookup_ranges
from agents.risk_flagger import classify_risk
from agents.explainer import explain
from agents.verifier import verify
from core.schemas import FinalExplanation

MAX_RETRIES = 1


async def run_pipeline(pdf_path: str) -> dict:
    """Run the full lab report pipeline on a PDF file.

    Returns dict with keys: explanations, raw_text, verified, issues (if any).
    """
    print(f"[orchestrator] Starting pipeline for: {pdf_path}")

    # Step 1: PDF → text (code-based, not LLM)
    print("[1/5] Extracting text from PDF...")
    raw_text = pdf_to_text(pdf_path)
    if not raw_text.strip():
        raise ValueError("PDF extraction returned empty text")
    print(f"      Extracted {len(raw_text)} characters")

    # Step 2: Text → structured values (LLM call #1)
    print("[2/5] Extracting lab values via LLM...")
    extracted = await extract_lab_values(raw_text)
    print(f"      Found {len(extracted)} lab values")

    # Step 3: Reference range lookup (tool call, NOT LLM)
    print("[3/5] Looking up reference ranges...")
    checked = lookup_ranges(extracted)
    print(f"      Range-checked {len(checked)} values")

    # Step 4: Risk classification (LLM call #2, grounded)
    print("[4/5] Classifying risk levels...")
    risk_flagged = await classify_risk(checked)
    print(f"      Classified {len(risk_flagged)} values")

    # Step 5: Explanation + verification with retry loop
    for attempt in range(MAX_RETRIES + 1):
        print(f"[5/5] Generating explanations (attempt {attempt + 1})...")
        explanations = await explain(risk_flagged)

        print("      Running verifier...")
        verification = await verify(explanations, risk_flagged)

        if verification.passed:
            print("      [PASS] Verifier PASSED")
            return {
                "explanations": [e.model_dump() for e in explanations],
                "raw_text": raw_text,
                "verified": True,
                "issues": [],
            }

        if attempt < MAX_RETRIES:
            print(f"      [FAIL] Verifier found issues: {verification.issues_found}")
            print("      Retrying with corrected prompt...")

    # After max retries — return with issues flagged
    print(f"      [FAIL] Verifier FAILED after {MAX_RETRIES + 1} attempts")
    return {
        "explanations": [e.model_dump() for e in explanations],
        "raw_text": raw_text,
        "verified": False,
        "issues": verification.issues_found,
    }


def run(pdf_path: str) -> dict:
    """Sync wrapper — call the async pipeline from sync code."""
    return asyncio.run(run_pipeline(pdf_path))
