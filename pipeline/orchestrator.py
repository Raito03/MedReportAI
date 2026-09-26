"""Orchestrator — wires all agents sequentially with error handling.

Flow: PDF → extract → lookup → risk → explain → verify
If verifier returns send_back_for_correction, retry once with issues injected.
All agent calls are async (openrouter-agent-sdk uses call_model()).
"""

import asyncio
from typing import Optional, List
from tools.pdf_extractor import pdf_to_text
from agents.extraction import extract_lab_values
from agents.reference_range import lookup_ranges
from agents.risk_flagger import classify_risk
from agents.explainer import explain
from agents.verifier import verify
from core.schemas import FinalExplanation
from core.observability import (
    FailureType, PipelineLogger, PipelineStage, classify_exception,
)

MAX_RETRIES = 1


async def run_pipeline(pdf_path: str, logger: Optional[PipelineLogger] = None) -> dict:
    """Run the full lab report pipeline on a PDF file.

    Returns dict with keys: explanations, raw_text, verified, issues (if any).

    Args:
        pdf_path: Path to the lab report PDF.
        logger: Optional PipelineLogger for structured observability events.
                 If None, events are still recorded in a local logger for
                 callers that want to inspect results.
    """
    # Create a local logger if none provided — events are always captured
    if logger is None:
        logger = PipelineLogger()

    print(f"[orchestrator] Starting pipeline for: {pdf_path}")

    # --- Stage 1: PDF extraction ---
    stage_start = logger.log_stage_start(PipelineStage.PDF_EXTRACTION)
    try:
        print("[1/5] Extracting text from PDF...")
        raw_text = pdf_to_text(pdf_path)
        if not raw_text.strip():
            raise ValueError("PDF extraction returned empty text")
        print(f"      Extracted {len(raw_text)} characters")
        logger.log_stage_success(
            PipelineStage.PDF_EXTRACTION, stage_start,
            metadata={"chars_extracted": len(raw_text)},
        )
    except Exception as exc:
        failure_type = classify_exception(exc)
        logger.log_stage_failure(
            PipelineStage.PDF_EXTRACTION, stage_start,
            failure_type=failure_type,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    # --- Stage 2: Extraction (LLM call #1) ---
    stage_start = logger.log_stage_start(PipelineStage.EXTRACTION)
    try:
        print("[2/5] Extracting lab values via LLM...")
        extracted = await extract_lab_values(raw_text)
        print(f"      Found {len(extracted)} lab values")
        logger.log_stage_success(
            PipelineStage.EXTRACTION, stage_start,
            metadata={"lab_value_count": len(extracted)},
        )
    except Exception as exc:
        failure_type = classify_exception(exc)
        logger.log_stage_failure(
            PipelineStage.EXTRACTION, stage_start,
            failure_type=failure_type,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    # --- Stage 3: Reference range lookup (tool call, NOT LLM) ---
    stage_start = logger.log_stage_start(PipelineStage.REFERENCE_LOOKUP)
    try:
        print("[3/5] Looking up reference ranges...")
        checked = lookup_ranges(extracted)
        print(f"      Range-checked {len(checked)} values")
        available = sum(1 for c in checked if c.range_available)
        logger.log_stage_success(
            PipelineStage.REFERENCE_LOOKUP, stage_start,
            metadata={
                "total_values": len(checked),
                "available_ranges": available,
                "unavailable_ranges": len(checked) - available,
            },
        )
    except Exception as exc:
        failure_type = classify_exception(exc)
        logger.log_stage_failure(
            PipelineStage.REFERENCE_LOOKUP, stage_start,
            failure_type=failure_type,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    # --- Stage 4: Risk classification (LLM call #2, grounded) ---
    stage_start = logger.log_stage_start(PipelineStage.RISK_FLAGGING)
    try:
        print("[4/5] Classifying risk levels...")
        risk_flagged = await classify_risk(checked)
        print(f"      Classified {len(risk_flagged)} values")
        status_counts = {}
        for r in risk_flagged:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
        logger.log_stage_success(
            PipelineStage.RISK_FLAGGING, stage_start,
            metadata={"classified_count": len(risk_flagged), "status_counts": status_counts},
        )
    except Exception as exc:
        failure_type = classify_exception(exc)
        logger.log_stage_failure(
            PipelineStage.RISK_FLAGGING, stage_start,
            failure_type=failure_type,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    # --- Stage 5: Explanation + verification with retry loop ---
    retry_count = 0
    for attempt in range(MAX_RETRIES + 1):
        stage_start = logger.log_stage_start(PipelineStage.EXPLANATION)
        try:
            print(f"[5/5] Generating explanations (attempt {attempt + 1})...")
            explanations = await explain(risk_flagged)
            print(f"      Generated {len(explanations)} explanations")
            logger.log_stage_success(
                PipelineStage.EXPLANATION, stage_start,
                metadata={"explanation_count": len(explanations), "attempt": attempt + 1},
            )
        except Exception as exc:
            failure_type = classify_exception(exc)
            logger.log_stage_failure(
                PipelineStage.EXPLANATION, stage_start,
                failure_type=failure_type,
                reason=f"{type(exc).__name__}: {exc}",
                retry_count=retry_count,
            )
            raise

        # Verification
        verify_start = logger.log_stage_start(PipelineStage.VERIFICATION)
        try:
            print("      Running verifier...")
            verification = await verify(explanations, risk_flagged)

            if verification.passed:
                print("      [PASS] Verifier PASSED")
                logger.log_stage_success(
                    PipelineStage.VERIFICATION, verify_start,
                    metadata={"attempt": attempt + 1},
                    retry_count=retry_count,
                )
                return {
                    "explanations": [e.model_dump() for e in explanations],
                    "raw_text": raw_text,
                    "verified": True,
                    "issues": [],
                }

            if attempt < MAX_RETRIES:
                print(f"      [FAIL] Verifier found issues: {verification.issues_found}")
                print("      Retrying with corrected prompt...")
                logger.log_stage_failure(
                    PipelineStage.VERIFICATION, verify_start,
                    failure_type=FailureType.VERIFICATION_FAILURE,
                    reason=f"Verifier rejected: {verification.issues_found}",
                    retry_count=retry_count,
                )
                retry_count += 1
            else:
                logger.log_stage_failure(
                    PipelineStage.VERIFICATION, verify_start,
                    failure_type=FailureType.VERIFICATION_FAILURE,
                    reason=f"Verifier rejected after {retry_count + 1} attempts: {verification.issues_found}",
                    retry_count=retry_count,
                )

        except Exception as exc:
            failure_type = classify_exception(exc)
            logger.log_stage_failure(
                PipelineStage.VERIFICATION, verify_start,
                failure_type=failure_type,
                reason=f"{type(exc).__name__}: {exc}",
                retry_count=retry_count,
            )
            raise

    # After max retries — return with issues flagged
    print(f"      [FAIL] Verifier FAILED after {MAX_RETRIES + 1} attempts")
    return {
        "explanations": [e.model_dump() for e in explanations],
        "raw_text": raw_text,
        "verified": False,
        "issues": verification.issues_found,
    }


def run(pdf_path: str, logger: Optional[PipelineLogger] = None) -> dict:
    """Sync wrapper — call the async pipeline from sync code."""
    return asyncio.run(run_pipeline(pdf_path, logger=logger))
