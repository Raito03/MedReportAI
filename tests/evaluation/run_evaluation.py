"""Run the P1-T3 Synthea extraction-accuracy evaluation (offline, deterministic).

Usage:
    python tests/evaluation/run_evaluation.py
    python tests/evaluation/run_evaluation.py --json evaluation_result.json
    python tests/evaluation/run_evaluation.py --check-fixtures
    python tests/evaluation/run_evaluation.py --fail-on-fail

The run is fully offline: the report PDFs are read with the project's real PDF
extractor, the project's real extraction agent runs, and only the LLM call is
replaced by the deterministic stand-in in ``synthea_extraction.py``. No API key
is required and no network request is made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.evaluation import pdf_fixtures  # noqa: E402
from tests.evaluation.evaluator import (  # noqa: E402
    GROUND_TRUTH_PATH,
    evaluate_dataset,
    format_text,
    load_ground_truth,
    reports_dir,
)
from tests.evaluation.generate_synthea_reports import MANIFEST_PATH  # noqa: E402
from tests.evaluation.synthea_extraction import extract_dataset  # noqa: E402


def run(ground_truth_path: Path = GROUND_TRUTH_PATH, reports_path: Path = None,
        target: float = None):
    """Extract every report of the ground truth and evaluate the result."""
    ground_truth_path = Path(ground_truth_path)
    ground_truth = load_ground_truth(ground_truth_path)
    directory = Path(reports_path) if reports_path else reports_dir(ground_truth_path.parent)
    by_report, runs = extract_dataset(ground_truth, directory)
    evaluation = evaluate_dataset(ground_truth, by_report, target=target)
    return evaluation, runs


def check_fixtures() -> bool:
    """Compare the committed PDFs against the manifest hashes."""
    if not MANIFEST_PATH.exists():
        print(f"FAIL: manifest missing: {MANIFEST_PATH}")
        return False
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    ok = True
    for entry in manifest["reports"]:
        pdf_path = reports_dir() / entry["pdf_file"]
        if not pdf_path.exists():
            print(f"FAIL: missing fixture {pdf_path}")
            ok = False
            continue
        digest = pdf_fixtures.sha256_of(pdf_path)
        if digest != entry["sha256"]:
            print(f"FAIL: {entry['report_id']} hash mismatch "
                  f"(manifest {entry['sha256'][:12]}, file {digest[:12]})")
            ok = False
    if ok:
        print(f"OK: {len(manifest['reports'])} report fixtures match the manifest")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", default=str(GROUND_TRUTH_PATH))
    parser.add_argument("--reports-dir", default=None)
    parser.add_argument("--target", type=float, default=None)
    parser.add_argument("--json", default=None, help="write machine-readable results here")
    parser.add_argument("--check-fixtures", action="store_true",
                        help="verify committed PDF hash manifest and exit")
    parser.add_argument("--fail-on-fail", action="store_true",
                        help="exit with status 1 when the evaluation does not pass")
    args = parser.parse_args(argv)

    if args.check_fixtures:
        return 0 if check_fixtures() else 1

    evaluation, runs = run(
        ground_truth_path=Path(args.ground_truth),
        reports_path=Path(args.reports_dir) if args.reports_dir else None,
        target=args.target,
    )

    print(format_text(evaluation))
    print("")
    print("Provenance:")
    print(f"  Offline: no network access, no OpenRouter API key required")
    print(f"  PDF stage: tools.pdf_extractor.pdf_to_text() read "
          f"{len(runs)} real PDF file(s)")
    pdf_text_visible = sum(1 for run in runs.values() if run.prompt_carried_pdf_text)
    print(f"  Extraction prompts carrying the extracted PDF text: "
          f"{pdf_text_visible}/{len(runs)}")
    print(f"  LLM calls (deterministic stand-in, zero real calls): "
          f"{sum(run.llm_calls for run in runs.values())}")

    if args.json:
        payload = evaluation.to_dict()
        payload["provenance"] = {
            "offline": True,
            "real_llm_calls": 0,
            "reports": len(runs),
            "prompts_carrying_pdf_text": pdf_text_visible,
            "mocked_llm_calls": sum(run.llm_calls for run in runs.values()),
        }
        with open(Path(args.json), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"  JSON results written to {args.json}")

    if args.fail_on_fail and not evaluation.passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
