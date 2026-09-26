"""Generate (or verify) the deterministic Synthea-style report PDFs (P1-T3).

Usage:
    python tests/evaluation/generate_synthea_reports.py            # write PDFs + manifest
    python tests/evaluation/generate_synthea_reports.py --check     # regenerate into a
                                                                   # temp dir and compare
                                                                   # hashes with the manifest

The PDFs are rendered from ``data/synthea_ground_truth.json`` (never from
extractor output) with reportlab's ``invariant`` mode, so the files are
byte-reproducible and safe to commit as fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.evaluation import pdf_fixtures  # noqa: E402
from tests.evaluation.evaluator import (  # noqa: E402
    DATA_DIR,
    GROUND_TRUTH_PATH,
    load_ground_truth,
    reports_dir,
)

MANIFEST_PATH = DATA_DIR / "synthea_reports_manifest.json"


def _write_manifest(manifest: dict) -> Path:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return MANIFEST_PATH


def generate(check: bool = False, out_dir: Path = None) -> int:
    ground_truth = load_ground_truth(GROUND_TRUTH_PATH)
    if check:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pdf_fixtures.generate_all(ground_truth, tmp_path)
            fresh = pdf_fixtures.manifest_for(ground_truth, tmp_path)
        if not MANIFEST_PATH.exists():
            print(f"FAIL: manifest missing: {MANIFEST_PATH}")
            return 1
        with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
            committed = json.load(handle)
        committed_hashes = {entry["report_id"]: entry["sha256"]
                            for entry in committed["reports"]}
        fresh_hashes = {entry["report_id"]: entry["sha256"]
                        for entry in fresh["reports"]}
        mismatches = [
            report_id for report_id, digest in fresh_hashes.items()
            if committed_hashes.get(report_id) != digest
        ]
        if mismatches:
            print("FAIL: regenerated PDFs differ from the committed fixtures: "
                  + ", ".join(sorted(mismatches)))
            return 1
        print(f"OK: {len(fresh_hashes)} report PDFs are byte-reproducible "
              "(generator output == committed fixtures)")
        return 0

    target_dir = Path(out_dir) if out_dir is not None else reports_dir()
    generated = pdf_fixtures.generate_all(ground_truth, target_dir)
    manifest = pdf_fixtures.manifest_for(ground_truth, target_dir)
    _write_manifest(manifest)
    print(f"Wrote {len(generated)} report PDFs to {target_dir}")
    print(f"Wrote manifest to {MANIFEST_PATH}")
    for entry in manifest["reports"]:
        print(f"  {entry['report_id']:<22} {entry['layout']:<20} "
              f"{entry['observation_count']:>2} obs  {entry['size_bytes']:>6} B  "
              f"{entry['sha256'][:12]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify that the committed PDFs are byte-reproducible")
    parser.add_argument("--out-dir", default=None,
                        help="directory to write the PDFs into (default: data/synthea_reports)")
    args = parser.parse_args(argv)
    return generate(check=args.check,
                    out_dir=Path(args.out_dir) if args.out_dir else None)


if __name__ == "__main__":
    raise SystemExit(main())
