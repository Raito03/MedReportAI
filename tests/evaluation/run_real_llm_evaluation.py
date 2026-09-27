"""P1-T3 — Real OpenRouter LLM extraction-accuracy evaluation.

Runs the same 10 Synthea reports through the REAL extraction path
(PDF -> pdfplumber -> agents.extraction.extract_lab_values -> REAL OpenRouter LLM)
and measures accuracy against ground truth.

Usage:
    python tests/evaluation/run_real_llm_evaluation.py
    python tests/evaluation/run_real_llm_evaluation.py --model cohere/north-mini-code:free
    python tests/evaluation/run_real_llm_evaluation.py --json real_llm_result.json

Requires OPENROUTER_API_KEY in .env.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import OPENROUTER_API_KEY, OPENROUTER_MODEL
from tests.evaluation.evaluator import (
    GROUND_TRUTH_PATH,
    evaluate_dataset,
    format_text,
    load_ground_truth,
    reports_dir,
)
from tools.pdf_extractor import pdf_to_text
from agents.extraction import extract_lab_values, build_extraction_prompt
from core.schemas import ExtractedLabValue


async def extract_with_real_llm(pdf_path: Path, report_id: str) -> dict:
    """Run real extraction on one PDF via the actual LLM."""
    raw_text = pdf_to_text(str(pdf_path))
    observations = await extract_lab_values(raw_text)
    # Convert to evaluator-expected format: list of dicts with REQUIRED_FIELDS
    obs_dicts = []
    for obs in observations:
        obs_dicts.append({
            "test_name": obs.test_name,
            "loinc_code": obs.loinc_code,
            "value": obs.value,
            "unit": obs.unit,
        })
    return {
        "report_id": report_id,
        "raw_text": raw_text,
        "observations": obs_dicts,
        "observation_count": len(obs_dicts),
    }


async def run_real_evaluation(
    ground_truth_path: Path = GROUND_TRUTH_PATH,
    reports_path: Path = None,
    model: str = None,
) -> dict:
    """Run real LLM evaluation on all Synthea reports."""
    ground_truth = load_ground_truth(ground_truth_path)
    directory = reports_path or reports_dir(ground_truth_path.parent)

    if model:
        # Temporarily override model
        import core.config
        original_model = core.config.OPENROUTER_MODEL
        core.config.OPENROUTER_MODEL = model
        try:
            return await _run_evaluation(ground_truth, directory)
        finally:
            core.config.OPENROUTER_MODEL = original_model
    else:
        return await _run_evaluation(ground_truth, directory)


async def _run_evaluation(ground_truth: dict, directory: Path) -> dict:
    """Internal: run real LLM extraction + evaluation."""
    results = {}
    extraction_errors = []
    start_time = time.time()

    for report in ground_truth["reports"]:
        report_id = report["report_id"]
        pdf_path = directory / f"{report_id}.pdf"

        if not pdf_path.exists():
            print(f"  SKIP: {report_id} - PDF not found")
            continue

        print(f"  Processing {report_id}...")
        try:
            result = await extract_with_real_llm(pdf_path, report_id)
            results[report_id] = result["observations"]
            print(f"    Extracted {result['observation_count']} observations")
        except Exception as e:
            print(f"    ERROR: {e}")
            extraction_errors.append({"report_id": report_id, "error": str(e)})
            results[report_id] = []

    elapsed = time.time() - start_time

    # Build the expected format for evaluate_dataset
    by_report = {}
    for report in ground_truth["reports"]:
        report_id = report["report_id"]
        by_report[report_id] = results.get(report_id, [])

    evaluation = evaluate_dataset(ground_truth, by_report)

    return {
        "evaluation": evaluation,
        "extraction_errors": extraction_errors,
        "elapsed_seconds": elapsed,
        "model": OPENROUTER_MODEL,
    }


def format_results(result: dict) -> str:
    """Format real LLM evaluation results."""
    eval_result = result["evaluation"]
    lines = [
        "Synthea Extraction Accuracy Evaluation (REAL LLM)",
        "=" * 50,
        "",
        f"Model: {result['model']}",
        f"Expected observations: {eval_result.expected_total}",
        f"Extracted observations: {eval_result.extracted_total}",
        "",
        "Field accuracy:",
    ]

    for field_name in ["test_name", "loinc_code", "value", "unit"]:
        correct, total = eval_result.field_totals(field_name)
        accuracy = eval_result.field_accuracy(field_name) * 100
        lines.append(f"  {field_name:12s}: {accuracy:.2f}%  ({correct}/{total})")

    lines.extend([
        "",
        "Observation accuracy:",
        f"  {eval_result.fully_correct_total}/{eval_result.expected_total} = {eval_result.observation_accuracy*100:.2f}%",
        "",
        f"Extraction precision: {eval_result.extraction_precision*100:.2f}%",
        f"Missing observations: {eval_result.missing_total}",
        f"Unexpected (hallucinated) observations: {eval_result.extra_total}",
        "",
        f"Target: >= 95.00%",
        f"RESULT: {'PASS' if eval_result.passed else 'FAIL'}",
        "",
        f"Time elapsed: {result['elapsed_seconds']:.1f}s",
        f"Model: {result['model']}",
    ])

    if result["extraction_errors"]:
        lines.append("")
        lines.append("Extraction errors:")
        for err in result["extraction_errors"]:
            lines.append(f"  {err['report_id']}: {err['error']}")

    return "\n".join(lines)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="Override model")
    parser.add_argument("--json", default=None, help="Write JSON results")
    args = parser.parse_args(argv)

    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set in .env")
        return 1

    print(f"Running real LLM evaluation with model: {args.model or OPENROUTER_MODEL}")
    print(f"API key present: {'yes' if OPENROUTER_API_KEY else 'NO'}")
    print()

    result = asyncio.run(run_real_evaluation(model=args.model))
    print(format_results(result))

    if args.json:
        payload = {
            "model": result["model"],
            "elapsed_seconds": result["elapsed_seconds"],
            "extraction_errors": result["extraction_errors"],
            "evaluation": {
                "expected_total": result["evaluation"].expected_total,
                "extracted_total": result["evaluation"].extracted_total,
                "observation_accuracy": result["evaluation"].observation_accuracy,
                "field_accuracy": {
                    field: result["evaluation"].field_accuracy(field)
                    for field in ["test_name", "loinc_code", "value", "unit"]
                },
                "missing_count": result["evaluation"].missing_count,
                "unexpected_count": result["evaluation"].unexpected_count,
                "passed": result["evaluation"].passed,
            },
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nJSON results written to {args.json}")

    return 0 if result["evaluation"].passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
