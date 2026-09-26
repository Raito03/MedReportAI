"""P1-T3 — Synthea extraction-accuracy evaluator (deterministic, offline).

WHAT THIS MEASURES
------------------
The evaluator compares the *expected* laboratory observations of a separately
stored synthetic ground truth (``data/synthea_ground_truth.json``) against the
observations produced by the project's real extraction path:

    PDF file  -> tools.pdf_extractor.pdf_to_text()          (real, code-based)
              -> agents.extraction.extract_lab_values()     (real agent code;
                 only the LLM call is replaced by a deterministic offline
                 stand-in, see tests/evaluation/synthea_extraction.py)
              -> core.schemas.ExtractedLabValue

Nothing in this module reaches the network: the evaluation runs offline and
does not need an OpenRouter API key.

METRICS (mirrored in the ``metric_definition`` block of the ground truth)
------------------------------------------------------------------------
field accuracy        (# expected observations whose matched extracted
                       observation has that field equal) / (# expected)
observation accuracy  (# expected observations correct on ALL four required
                       fields) / (# expected)  <- headline metric, target 95%
extraction precision  (# fully correct extracted observations) / (# extracted)

A missing observation counts as a failure for every field. An unexpected
(hallucinated) observation never counts as a match and additionally fails the
run: inventing a lab result is safety-relevant and must not be hidden behind a
high match rate.

MATCHING (deterministic, order-safe, duplicate-safe)
----------------------------------------------------
1. identity match   : expected and extracted observations are grouped by
   (normalized test_name, loinc_code) — stable identity, not the name alone —
   and matched k-th occurrence to k-th occurrence in document order, so two
   "Potassium" rows can never be crossed with each other.
2. diagnostic pairing: expected observations without an identity partner are
   paired (again in occurrence order) with a still-unmatched extracted
   observation of the same normalized test name, and then with one sharing the
   same LOINC code. A wrong/missing LOINC or a wrong test name or value is then
   reported as a FIELD mismatch instead of a confusing missing + unexpected
   pair. Such a pair never counts as correct: all four fields still have to
   match.
3. leftovers        : remaining expected -> MISSING, remaining extracted ->
   UNEXPECTED.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The project's EXISTING unit rules are reused instead of inventing new
# conversions: a unit pair is either identical, provably equivalent through the
# project's own conversion/alias table, or not comparable.
from agents.reference_range import _get_conversion_factor

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
DATA_DIR = EVAL_DIR / "data"
GROUND_TRUTH_PATH = DATA_DIR / "synthea_ground_truth.json"
REPORTS_DIR_NAME = "synthea_reports"

TARGET_ACCURACY = 0.95
REQUIRED_FIELDS: Tuple[str, ...] = ("test_name", "loinc_code", "value", "unit")

# Numeric equivalence: 5 == 5.0 == 5.00, but 13.4 != 13.0.
VALUE_ABS_TOL = 1e-9

# Unit comparison outcomes.
UNIT_EXACT = "exact"
UNIT_EQUIVALENT = "equivalent"
UNIT_DIFFERENT = "different"


# ---------------------------------------------------------------------------
# Ground truth loading
# ---------------------------------------------------------------------------

def load_ground_truth(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the separately stored synthetic ground truth (never extractor output)."""
    gt_path = Path(path) if path is not None else GROUND_TRUTH_PATH
    with open(gt_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not data.get("reports"):
        raise ValueError(f"Ground truth contains no reports: {gt_path}")
    return data


def reports_dir(data_dir: Optional[Path] = None) -> Path:
    """Directory holding the deterministic report PDFs."""
    return (Path(data_dir) if data_dir is not None else DATA_DIR) / REPORTS_DIR_NAME




    return reports_dir(data_dir) / f"{report['report_id']}.pdf"


# ---------------------------------------------------------------------------
# Field comparators
# ---------------------------------------------------------------------------

def normalize_test_name(name: Any) -> str:
    """Case-insensitive, whitespace-collapsed test-name key."""
    return " ".join(str(name).split()).casefold()


def normalize_loinc(code: Any) -> str:
    """LOINC identity key (exact, whitespace stripped only)."""
    return str(code or "").strip()


def normalize_unit(unit: Any) -> str:
    """Unit key: case-insensitive, whitespace-collapsed, micro sign unified."""
    return (
        " ".join(str(unit or "").split())
        .lower()
        .replace("\u00b5", "u")  # MICRO SIGN
        .replace("\u03bc", "u")  # GREEK SMALL LETTER MU
    )


def values_equal(expected: Any, extracted: Any) -> bool:
    """Numeric equivalence with float coercion (5 == 5.0 == 5.00)."""
    try:
        left = float(expected)
        right = float(extracted)
    except (TypeError, ValueError):
        return False
    if math.isnan(left) or math.isnan(right):
        return False
    return math.isclose(left, right, rel_tol=0.0, abs_tol=VALUE_ABS_TOL)


def unit_match_kind(expected: Any, extracted: Any) -> str:
    """Classify a unit pair using the project's existing unit rules only."""
    if normalize_unit(expected) == normalize_unit(extracted):
        return UNIT_EXACT
    factor = _get_conversion_factor(str(expected or ""), str(extracted or ""))
    if factor is not None:
        return UNIT_EQUIVALENT
    return UNIT_DIFFERENT


def field_matches(expected_obs: Dict[str, Any], extracted_obs: Dict[str, Any],
                  field_name: str) -> Tuple[bool, str]:
    """Return (is_match, kind) for one field of a matched observation pair."""
    expected_value = expected_obs.get(field_name)
    extracted_value = extracted_obs.get(field_name)

    if field_name == "test_name":
        return test_names_equal(expected_value, extracted_value), UNIT_EXACT
    if field_name == "loinc_code":
        return loinc_codes_equal(expected_value, extracted_value), UNIT_EXACT
    if field_name == "value":
        return values_equal(expected_value, extracted_value), UNIT_EXACT
    if field_name == "unit":
        kind = unit_match_kind(expected_value, extracted_value)
        return kind != UNIT_DIFFERENT, kind
    raise KeyError(f"Unknown field: {field_name}")


def test_names_equal(expected: Any, extracted: Any) -> bool:
    return normalize_test_name(expected) == normalize_test_name(extracted)


def loinc_codes_equal(expected: Any, extracted: Any) -> bool:
    return normalize_loinc(expected) == normalize_loinc(extracted)


# ---------------------------------------------------------------------------
# Observation normalization
# ---------------------------------------------------------------------------

def observation_to_dict(observation: Any) -> Dict[str, Any]:
    """Normalize an extracted observation (pydantic model, dataclass or dict)."""
    if isinstance(observation, dict):
        data = dict(observation)
    elif hasattr(observation, "model_dump"):
        data = observation.model_dump()
    elif hasattr(observation, "__dict__"):
        data = dict(vars(observation))
    else:
        raise TypeError(f"Cannot normalize observation of type {type(observation)!r}")
    return {name: data.get(name) for name in REQUIRED_FIELDS}


def expected_observations_to_dicts(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten one ground-truth report into comparable expected observations.

    ``loinc_code`` is the LOINC the finished pipeline is expected to report: the
    printed code when the report carries one, otherwise the code the project's
    supported-labs resolution must produce from the test name.
    """
    report_id = report["report_id"]
    observations = []
    for index, expected in enumerate(report["expected_observations"], start=1):
        observation = {name: expected.get(name) for name in REQUIRED_FIELDS}
        observation["observation_id"] = expected.get(
            "observation_id", f"{report_id}#{expected.get('order', index)}"
        )
        observation["order"] = expected.get("order", index)
        observations.append(observation)
    return observations


# ---------------------------------------------------------------------------
# Deterministic matching
# ---------------------------------------------------------------------------

@dataclass
class ObservationPair:
    """One expected observation and the extracted observation it was paired with."""
    report_id: str
    expected: Dict[str, Any]
    extracted: Optional[Dict[str, Any]]
    match_kind: str  # "identity" | "name_only" | "missing"
    field_results: Dict[str, bool] = dataclass_field(default_factory=dict)
    field_kinds: Dict[str, str] = dataclass_field(default_factory=dict)

    @property
    def expected_id(self) -> str:
        return str(self.expected.get("observation_id", ""))

    @property
    def is_fully_correct(self) -> bool:
        if self.extracted is None:
            return False
        return all(self.field_results.get(name, False) for name in REQUIRED_FIELDS)

    @property
    def mismatched_fields(self) -> List[str]:
        return [name for name in REQUIRED_FIELDS if not self.field_results.get(name, False)]

    @property
    def unit_matched_by_equivalence(self) -> bool:
        return self.field_kinds.get("unit") == UNIT_EQUIVALENT


@dataclass
class ExtraObservation:
    """An extracted observation that no expected observation accounts for."""
    report_id: str
    extracted: Dict[str, Any]


@dataclass
class ReportEvaluation:
    """Evaluation result of a single synthetic report."""
    report_id: str
    panel: str
    expected_count: int
    extracted_count: int
    pairs: List[ObservationPair]
    extras: List[ExtraObservation]

    @property
    def fully_correct_count(self) -> int:
        return sum(1 for pair in self.pairs if pair.is_fully_correct)

    @property
    def missing_count(self) -> int:
        return sum(1 for pair in self.pairs if pair.extracted is None)

    @property
    def mismatched_count(self) -> int:
        return sum(1 for pair in self.pairs if not pair.is_fully_correct)

    def field_totals(self, field_name: str) -> Tuple[int, int]:
        matched = sum(1 for pair in self.pairs if pair.field_results.get(field_name, False))
        return matched, len(self.pairs)


def match_observations(report_id: str, expected_list: Sequence[Dict[str, Any]],
                       extracted_list: Sequence[Dict[str, Any]]
                       ) -> Tuple[List[ObservationPair], List[ExtraObservation]]:
    """Deterministically pair expected and extracted observations (see module docstring)."""
    matched_extracted = [False] * len(extracted_list)
    pairs: List[ObservationPair] = []

    # Phase 1 — stable identity (normalized test name + LOINC), occurrence order.
    identity_groups: Dict[Tuple[str, str], List[int]] = {}
    for index, extracted in enumerate(extracted_list):
        key = (normalize_test_name(extracted.get("test_name")),
               normalize_loinc(extracted.get("loinc_code")))
        identity_groups.setdefault(key, []).append(index)
    identity_cursor: Dict[Tuple[str, str], int] = {}

    for expected in expected_list:
        key = (normalize_test_name(expected.get("test_name")),
               normalize_loinc(expected.get("loinc_code")))
        group = identity_groups.get(key, [])
        position = identity_cursor.get(key, 0)
        if position < len(group):
            extracted_index = group[position]
            identity_cursor[key] = position + 1
            matched_extracted[extracted_index] = True
            pairs.append(ObservationPair(
                report_id, expected, extracted_list[extracted_index], "identity"))
        else:
            pairs.append(ObservationPair(report_id, expected, None, "missing"))

    # Phase 2 — diagnostic name-only pairing, so a wrong LOINC or a wrong value
    # is reported as a FIELD mismatch instead of missing + unexpected.
    name_groups: Dict[str, List[int]] = {}
    for index, extracted in enumerate(extracted_list):
        if not matched_extracted[index]:
            name_groups.setdefault(
                normalize_test_name(extracted.get("test_name")), []).append(index)
    name_cursor: Dict[str, int] = {}

    for pair in pairs:
        if pair.extracted is not None:
            continue
        name_key = normalize_test_name(pair.expected.get("test_name"))
        group = name_groups.get(name_key, [])
        position = name_cursor.get(name_key, 0)
        if position < len(group):
            extracted_index = group[position]
            name_cursor[name_key] = position + 1
            matched_extracted[extracted_index] = True
            pair.extracted = extracted_list[extracted_index]
            pair.match_kind = "name_only"

    # Phase 3 — diagnostic LOINC-only pairing, so a wrong test name that still
    # carries the right LOINC is reported as a test_name mismatch.
    loinc_groups: Dict[str, List[int]] = {}
    for index, extracted in enumerate(extracted_list):
        if not matched_extracted[index]:
            loinc_groups.setdefault(
                normalize_loinc(extracted.get("loinc_code")), []).append(index)
    loinc_cursor: Dict[str, int] = {}

    for pair in pairs:
        if pair.extracted is not None:
            continue
        loinc_key = normalize_loinc(pair.expected.get("loinc_code"))
        group = loinc_groups.get(loinc_key, [])
        position = loinc_cursor.get(loinc_key, 0)
        if position < len(group):
            extracted_index = group[position]
            loinc_cursor[loinc_key] = position + 1
            matched_extracted[extracted_index] = True
            pair.extracted = extracted_list[extracted_index]
            pair.match_kind = "loinc_only"

    # Phase 4 — leftovers.
    extras = [
        ExtraObservation(report_id, extracted)
        for index, extracted in enumerate(extracted_list)
        if not matched_extracted[index]
    ]
    return pairs, extras


def score_pairs(pairs: Iterable[ObservationPair]) -> None:
    """Fill in the per-field comparison results of every pair (in place)."""
    for pair in pairs:
        if pair.extracted is None:
            pair.field_results = {name: False for name in REQUIRED_FIELDS}
            pair.field_kinds = {name: UNIT_DIFFERENT for name in REQUIRED_FIELDS}
            continue
        results: Dict[str, bool] = {}
        kinds: Dict[str, str] = {}
        for name in REQUIRED_FIELDS:
            is_match, kind = field_matches(pair.expected, pair.extracted, name)
            results[name] = is_match
            kinds[name] = kind
        pair.field_results = results
        pair.field_kinds = kinds


def evaluate_report(report: Dict[str, Any],
                    extracted_observations: Sequence[Any]) -> ReportEvaluation:
    """Evaluate one ground-truth report against the observations extracted from it."""
    expected_list = expected_observations_to_dicts(report)
    extracted_list = [observation_to_dict(item) for item in extracted_observations]
    pairs, extras = match_observations(report["report_id"], expected_list, extracted_list)
    score_pairs(pairs)
    return ReportEvaluation(
        report_id=report["report_id"],
        panel=report.get("panel", ""),
        expected_count=len(expected_list),
        extracted_count=len(extracted_list),
        pairs=pairs,
        extras=extras,
    )


# ---------------------------------------------------------------------------
# Dataset-level metrics and reporting
# ---------------------------------------------------------------------------

FIELD_LABELS = {
    "test_name": "Test name",
    "loinc_code": "LOINC    ",
    "value": "Value    ",
    "unit": "Unit     ",
}


@dataclass
class EvaluationReport:
    """Aggregated result of evaluating a whole synthetic dataset."""
    dataset_name: str
    target: float
    reports: List[ReportEvaluation]

    # --- counts -----------------------------------------------------------
    @property
    def expected_total(self) -> int:
        return sum(report.expected_count for report in self.reports)

    @property
    def extracted_total(self) -> int:
        return sum(report.extracted_count for report in self.reports)

    @property
    def fully_correct_total(self) -> int:
        return sum(report.fully_correct_count for report in self.reports)

    @property
    def missing_total(self) -> int:
        return sum(report.missing_count for report in self.reports)

    @property
    def extra_total(self) -> int:
        return sum(len(report.extras) for report in self.reports)

    @property
    def unit_equivalence_matches(self) -> int:
        return sum(
            1 for report in self.reports for pair in report.pairs
            if pair.unit_matched_by_equivalence
        )

    def all_pairs(self) -> List[Tuple[ReportEvaluation, ObservationPair]]:
        return [(report, pair) for report in self.reports for pair in report.pairs]

    # --- accuracy ---------------------------------------------------------
    def field_totals(self, field_name: str) -> Tuple[int, int]:
        matched = 0
        total = 0
        for report in self.reports:
            report_matched, report_total = report.field_totals(field_name)
            matched += report_matched
            total += report_total
        return matched, total

    def field_accuracy(self, field_name: str) -> float:
        matched, total = self.field_totals(field_name)
        return (matched / total) if total else 0.0

    @property
    def observation_accuracy(self) -> float:
        return (self.fully_correct_total / self.expected_total) if self.expected_total else 0.0

    @property
    def extraction_precision(self) -> float:
        return (self.fully_correct_total / self.extracted_total) if self.extracted_total else 0.0

    @property
    def failed_observations(self) -> List[Tuple[ReportEvaluation, ObservationPair]]:
        return [(report, pair) for report, pair in self.all_pairs()
                if not pair.is_fully_correct]

    # --- verdict ----------------------------------------------------------
    def failure_reasons(self) -> List[str]:
        reasons = []
        if self.observation_accuracy < self.target:
            reasons.append(
                f"observation accuracy {self.observation_accuracy:.2%} < target {self.target:.2%}")
        for field_name in REQUIRED_FIELDS:
            accuracy = self.field_accuracy(field_name)
            if accuracy < self.target:
                reasons.append(
                    f"{field_name} accuracy {accuracy:.2%} < target {self.target:.2%}")
        if self.extra_total:
            reasons.append(
                f"{self.extra_total} unexpected observation(s) not present in the ground truth")
        return reasons

    @property
    def passed(self) -> bool:
        return not self.failure_reasons()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_name": self.dataset_name,
            "target": self.target,
            "passed": self.passed,
            "failure_reasons": self.failure_reasons(),
            "counts": {
                "reports": len(self.reports),
                "expected": self.expected_total,
                "extracted": self.extracted_total,
                "fully_correct": self.fully_correct_total,
                "missing": self.missing_total,
                "unexpected": self.extra_total,
                "unit_equivalence_matches": self.unit_equivalence_matches,
            },
            "accuracy": {
                "observation": self.observation_accuracy,
                "extraction_precision": self.extraction_precision,
                "fields": {
                    name: {
                        "accuracy": self.field_accuracy(name),
                        "matched": self.field_totals(name)[0],
                        "total": self.field_totals(name)[1],
                    }
                    for name in REQUIRED_FIELDS
                },
            },
            "per_report": [
                {
                    "report_id": report.report_id,
                    "expected": report.expected_count,
                    "extracted": report.extracted_count,
                    "fully_correct": report.fully_correct_count,
                    "missing": report.missing_count,
                    "unexpected": len(report.extras),
                }
                for report in self.reports
            ],
        }


def evaluate_dataset(ground_truth: Dict[str, Any],
                     extracted_by_report: Dict[str, Sequence[Any]],
                     target: Optional[float] = None) -> EvaluationReport:
    """Evaluate every report of the ground truth against its extracted observations.

    ``extracted_by_report`` maps ``report_id`` -> observations produced by the real
    extraction path (``ExtractedLabValue`` objects or equivalent dicts).
    """
    target_accuracy = TARGET_ACCURACY if target is None else float(target)
    report_evaluations = []
    for report in ground_truth["reports"]:
        report_id = report["report_id"]
        extracted = extracted_by_report.get(report_id, [])
        report_evaluations.append(evaluate_report(report, extracted))
    return EvaluationReport(
        dataset_name=ground_truth.get("dataset_name", "synthea_ground_truth"),
        target=target_accuracy,
        reports=report_evaluations,
    )


# ---------------------------------------------------------------------------
# Human-readable failure report
# ---------------------------------------------------------------------------

def _format_observation_lines(observation: Optional[Dict[str, Any]],
                              indent: str = "  ") -> List[str]:
    if observation is None:
        return [f"{indent}<missing - no extracted observation matched this expected "
                "observation>"]
    return [f"{indent}{name}: {observation.get(name)}" for name in REQUIRED_FIELDS]


def format_pair_failure(report: ReportEvaluation, pair: ObservationPair) -> str:
    """Failure block for one expected observation that was not extracted correctly."""
    lines = [
        f"FAIL  id={pair.expected_id}  report={report.report_id}",
        f"Test: {pair.expected.get('test_name')}",
        "",
        "Expected:",
    ]
    lines.extend(_format_observation_lines(pair.expected))
    lines.append("")
    lines.append("Extracted:")
    lines.extend(_format_observation_lines(pair.extracted))
    lines.append("")
    lines.append("Mismatch:")
    if pair.extracted is None:
        lines.append("  observation missing (no extracted observation matched this test)")
    else:
        for name in pair.mismatched_fields:
            lines.append(f"  {name}: expected {pair.expected.get(name)!r}, "
                         f"extracted {pair.extracted.get(name)!r}")
    return "\n".join(lines)


def format_extra_failure(report: ReportEvaluation, extra: ExtraObservation) -> str:
    """Failure block for an extracted observation with no ground-truth counterpart."""
    lines = [
        f"UNEXPECTED  report={report.report_id}  (not present in the ground truth)",
        f"Test: {extra.extracted.get('test_name')}",
        "",
        "Extracted:",
    ]
    lines.extend(_format_observation_lines(extra.extracted))
    lines.append("")
    lines.append("Expected: <no ground-truth observation for this extraction>")
    lines.append("Mismatch:")
    lines.append("  unexpected observation (hallucinated or duplicated extraction)")
    return "\n".join(lines)


def format_text(evaluation: EvaluationReport) -> str:
    """Build the deterministic, printable evaluation report."""
    lines: List[str] = []
    lines.append("Synthea Extraction Accuracy Evaluation")
    lines.append("--------------------------------------")
    lines.append("")
    lines.append(f"Dataset: {evaluation.dataset_name}")
    lines.append(f"Reports evaluated: {len(evaluation.reports)}")
    lines.append(f"Expected observations: {evaluation.expected_total}")
    lines.append(f"Extracted observations: {evaluation.extracted_total}")
    lines.append("")
    lines.append("Field accuracy:")
    for name in REQUIRED_FIELDS:
        matched, total = evaluation.field_totals(name)
        accuracy = evaluation.field_accuracy(name)
        lines.append(f"  {FIELD_LABELS[name]}: {accuracy:6.2%}  ({matched}/{total})")
    if evaluation.unit_equivalence_matches:
        lines.append("  (units accepted through the project's unit-conversion table: "
                     f"{evaluation.unit_equivalence_matches})")
    lines.append("")
    lines.append("Observation accuracy:")
    lines.append(f"  {evaluation.fully_correct_total}/{evaluation.expected_total} = "
                 f"{evaluation.observation_accuracy:.2%}")
    lines.append("")
    lines.append(f"Extraction precision (fully correct / extracted): "
                 f"{evaluation.extraction_precision:.2%}")
    lines.append(f"Missing observations: {evaluation.missing_total}")
    lines.append(f"Unexpected (hallucinated) observations: {evaluation.extra_total}")

    failures = evaluation.failed_observations
    extras = [(report, extra) for report in evaluation.reports for extra in report.extras]
    if failures or extras:
        lines.append("")
        lines.append("Failures:")
        lines.append("")
        for report, pair in failures:
            lines.append(format_pair_failure(report, pair))
            lines.append("")
        for report, extra in extras:
            lines.append(format_extra_failure(report, extra))
            lines.append("")

    lines.append("Target:")
    lines.append(f"  >= {evaluation.target:.2%}")
    lines.append("")
    lines.append(f"RESULT: {'PASS' if evaluation.passed else 'FAIL'}")
    reasons = evaluation.failure_reasons()
    for reason in reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines)





