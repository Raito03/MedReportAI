"""P1-T3 — Synthea extraction-accuracy evaluation tests (offline, deterministic).

Seven required tests plus rigor checks:

1. perfect extraction reaches 100% on every metric
2. field-level mismatches (test name / LOINC / value / unit) are detected per field
3. a missing observation is counted as a failure (never silently ignored)
4. an unexpected/hallucinated observation is detected and fails the run
5. duplicate test names cannot cross-contaminate each other (occurrence order)
6. numeric equivalence (5 vs 5.0 vs 5.00, 0.90 vs 0.9) is handled explicitly
7. the full synthetic dataset goes through the REAL PDF extraction stage + the
   real extraction agent (mocked LLM only) and is measured against the 95% target

Rigor checks: ground-truth invariants (supported LOINCs/units only), fixture
byte-reproducibility, and the extracted observations still feeding the
project's reference-range lookup.

No test in this module touches the network: ``tests/conftest.py`` already
installs a guard that fails on any real LLM call, and the stand-in patches the
same injection point with an ``OfflineClient`` that raises if it is ever used.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.reference_range import lookup_ranges
from tests.evaluation import pdf_fixtures
from tests.evaluation.evaluator import (
    GROUND_TRUTH_PATH,
    REQUIRED_FIELDS,
    TARGET_ACCURACY,
    evaluate_dataset,
    evaluate_report,
    format_text,
    load_ground_truth,
    reports_dir,
    values_equal,
)
from tests.evaluation.generate_synthea_reports import MANIFEST_PATH
from tests.evaluation.synthea_extraction import (
    extract_dataset,
    extract_report_observations,
    unit_vocabulary,
)

GROUND_TRUTH = load_ground_truth(GROUND_TRUTH_PATH)
REPORTS = GROUND_TRUTH["reports"]
EXPECTED_OBSERVATIONS = sum(len(report["expected_observations"]) for report in REPORTS)
TARGET = GROUND_TRUTH["metric_definition"]["target"]


def _report(report_id: str) -> dict:
    for report in REPORTS:
        if report["report_id"] == report_id:
            return report
    raise KeyError(report_id)


def _perfect_extraction(report: dict):
    """What a flawless extraction of this report looks like (all four fields)."""
    return [{name: observation[name] for name in REQUIRED_FIELDS}
            for observation in report["expected_observations"]]


# ---------------------------------------------------------------------------
# 1. Perfect extraction
# ---------------------------------------------------------------------------

def test_perfect_extraction_scores_100_percent():
    extracted = {report["report_id"]: _perfect_extraction(report) for report in REPORTS}
    evaluation = evaluate_dataset(GROUND_TRUTH, extracted)

    assert evaluation.expected_total == EXPECTED_OBSERVATIONS
    assert evaluation.extracted_total == EXPECTED_OBSERVATIONS
    for field_name in REQUIRED_FIELDS:
        assert evaluation.field_accuracy(field_name) == 1.0, field_name
    assert evaluation.observation_accuracy == 1.0
    assert evaluation.extraction_precision == 1.0
    assert evaluation.missing_total == 0
    assert evaluation.extra_total == 0
    assert evaluation.passed is True
    print(f"  PASS: perfect extraction -> {evaluation.observation_accuracy:.2%} "
          f"observation accuracy over {EXPECTED_OBSERVATIONS} observations")


# ---------------------------------------------------------------------------
# 2. Field-level mismatches
# ---------------------------------------------------------------------------

def test_field_level_mismatches_are_detected_per_field():
    report = _report("synthea_cbc_0002")
    extracted = _perfect_extraction(report)
    extracted[0]["value"] = 13.0          # Hemoglobin value wrong
    extracted[1]["unit"] = "mg/dL"        # Hematocrit unit wrong
    extracted[2]["loinc_code"] = "1234-5"  # WBC LOINC wrong
    extracted[3]["test_name"] = "Red cell count"  # RBC name wrong

    evaluation = evaluate_report(report, extracted)
    results = [pair.field_results for pair in evaluation.pairs]

    assert results[0]["value"] is False
    assert all(results[0][name] for name in REQUIRED_FIELDS if name != "value")
    assert results[1]["unit"] is False
    assert results[2]["loinc_code"] is False
    assert results[3]["test_name"] is False
    assert results[3]["loinc_code"] is True
    assert evaluation.pairs[2].match_kind == "name_only"   # right name, wrong LOINC
    assert evaluation.pairs[3].match_kind == "loinc_only"  # right LOINC, wrong name
    assert evaluation.fully_correct_count == len(evaluation.pairs) - 4
    assert evaluation.missing_count == 0
    assert evaluation.extras == []
    assert evaluation.mismatched_count == 4

    text = format_text(evaluate_dataset(GROUND_TRUTH, {"synthea_cbc_0002": extracted}))
    assert "FAIL  id=synthea_cbc_0002#1" in text
    assert "value: expected 13.4, extracted 13.0" in text
    assert "unit: expected '%', extracted 'mg/dL'" in text
    print("  PASS: value/unit/LOINC/test-name mismatches each flagged individually")


def _dataset_extraction(**overrides):
    """Perfect extraction for every report, with selected reports replaced."""
    extracted = {report["report_id"]: _perfect_extraction(report) for report in REPORTS}
    extracted.update(overrides)
    return extracted


# ---------------------------------------------------------------------------
# 3. Missing observation
# ---------------------------------------------------------------------------

def test_missing_observation_is_counted_as_a_failure():
    report = _report("synthea_cmp_0001")
    extracted = _perfect_extraction(report)
    extracted.pop(3)  # Chloride was never extracted

    evaluation = evaluate_report(report, extracted)
    assert evaluation.missing_count == 1
    missing_pair = evaluation.pairs[3]
    assert missing_pair.expected_id == "synthea_cmp_0001#4"
    assert missing_pair.extracted is None
    assert missing_pair.field_results == {name: False for name in REQUIRED_FIELDS}
    assert evaluation.fully_correct_count == len(evaluation.pairs) - 1

    full = evaluate_dataset(
        GROUND_TRUTH, _dataset_extraction(synthea_cmp_0001=extracted))
    assert full.missing_total == 1
    assert full.observation_accuracy == (EXPECTED_OBSERVATIONS - 1) / EXPECTED_OBSERVATIONS
    text = format_text(full)
    assert "FAIL  id=synthea_cmp_0001#4" in text
    assert "observation missing" in text
    print("  PASS: missing observation counted as a failure and reported with its id")


# ---------------------------------------------------------------------------
# 4. Unexpected / hallucinated observation
# ---------------------------------------------------------------------------

def test_unexpected_observation_is_detected_and_fails_the_run():
    report = _report("synthea_lipid_0003")
    hallucination = {"test_name": "Mystery analyte", "loinc_code": "99999-9",
                     "value": 42.0, "unit": "U"}
    extracted = _perfect_extraction(report) + [hallucination]

    evaluation = evaluate_report(report, extracted)
    assert len(evaluation.extras) == 1
    assert evaluation.extras[0].extracted == hallucination

    full = evaluate_dataset(
        GROUND_TRUTH, _dataset_extraction(synthea_lipid_0003=extracted))
    assert full.extra_total == 1
    assert full.observation_accuracy == 1.0          # no expected value was missed
    assert full.extraction_precision < 1.0           # but the invention is punished
    assert full.passed is False
    assert any("unexpected" in reason for reason in full.failure_reasons())
    text = format_text(full)
    assert "UNEXPECTED  report=synthea_lipid_0003" in text
    assert "Mystery analyte" in text
    print("  PASS: hallucinated observation detected, reported and fails the run")


# ---------------------------------------------------------------------------
# 5. Duplicate test names
# ---------------------------------------------------------------------------

def test_duplicate_test_names_cannot_cross_contaminate():
    report = _report("synthea_duplicates_0005")
    perfect = _perfect_extraction(report)
    assert perfect[0]["test_name"] == perfect[1]["test_name"] == "Potassium"
    assert perfect[0]["value"] == 4.1 and perfect[1]["value"] == 3.9

    # Rows swapped by the extractor: occurrence-order matching must flag BOTH rows.
    swapped = [perfect[1], perfect[0]] + perfect[2:]
    evaluation = evaluate_report(report, swapped)
    assert evaluation.pairs[0].expected["value"] == 4.1
    assert evaluation.pairs[0].extracted["value"] == 3.9
    assert evaluation.pairs[0].field_results["value"] is False
    assert evaluation.pairs[1].field_results["value"] is False
    assert evaluation.fully_correct_count == 2  # only Sodium and CO2 remain correct
    assert evaluation.missing_count == 0

    # One duplicate dropped: the surviving duplicate still matches its own occurrence.
    dropped = [perfect[0]] + perfect[2:]
    evaluation = evaluate_report(report, dropped)
    assert evaluation.pairs[0].is_fully_correct
    assert evaluation.pairs[1].extracted is None
    assert evaluation.missing_count == 1
    print("  PASS: duplicate test names matched by occurrence order, never crossed")


# ---------------------------------------------------------------------------
# 6. Numeric equivalence
# ---------------------------------------------------------------------------

def test_numeric_equivalence_is_explicit_and_not_over_permissive():
    assert values_equal(5, 5.0)
    assert values_equal(5, 5.00)
    assert values_equal("13.40", 13.4)
    assert values_equal(92, "92.0")
    assert not values_equal(13.4, 13.0)
    assert not values_equal(5, "five")
    assert not values_equal(None, 5)
    assert not values_equal(92.0, 92.0000001)

    # The dataset itself contains printed forms like 0.90 / 4.20 / 1.20.
    report = _report("synthea_cmp_0001")
    creatinine = report["expected_observations"][5]
    assert creatinine["value"] == 0.9
    assert creatinine["printed_value_text"] == "0.90"
    extracted = _perfect_extraction(report)
    extracted[5]["value"] = 0.90
    evaluation = evaluate_report(report, extracted)
    assert evaluation.pairs[5].is_fully_correct
    print("  PASS: 5 == 5.0 == 5.00 accepted, 13.4 != 13.0 rejected")


# ---------------------------------------------------------------------------
# 7. Full dataset through the real extraction path
# ---------------------------------------------------------------------------

def test_full_synthea_dataset_meets_the_accuracy_target():
    by_report, runs = extract_dataset(GROUND_TRUTH, reports_dir())
    evaluation = evaluate_dataset(GROUND_TRUTH, by_report)

    assert set(runs) == {report["report_id"] for report in REPORTS}
    for report_id, run in runs.items():
        assert run.llm_calls == 1, report_id           # one mocked call per report
        assert run.prompt_carried_pdf_text, report_id  # pdfplumber text really used
        assert run.raw_text.strip()
        assert len(run.observations) == len(run.planned_response)

    assert EXPECTED_OBSERVATIONS >= 20
    assert evaluation.expected_total == EXPECTED_OBSERVATIONS
    assert TARGET == TARGET_ACCURACY
    assert evaluation.observation_accuracy >= TARGET_ACCURACY
    for field_name in REQUIRED_FIELDS:
        assert evaluation.field_accuracy(field_name) >= TARGET_ACCURACY, field_name
    assert evaluation.extra_total == 0
    assert evaluation.passed is True

    print("")
    print(format_text(evaluation))
    print("")


# ---------------------------------------------------------------------------
# Rigor checks: dataset integrity, fixture reproducibility, downstream usability
# ---------------------------------------------------------------------------

def test_ground_truth_contains_only_supported_labs_and_units():
    with open(Path(__file__).resolve().parents[2] / "data" / "supported_labs.json",
              "r", encoding="utf-8") as handle:
        supported = {test["loinc_code"]: test for test in json.load(handle)["tests"]}
    known_units = set(unit_vocabulary())

    unknown_loinc = []
    unknown_unit = []
    name_mismatch = []
    for report in REPORTS:
        for observation in report["expected_observations"]:
            code = observation["loinc_code"]
            if code not in supported:
                unknown_loinc.append((report["report_id"], code))
                continue
            if supported[code]["test_name"].casefold() != observation["test_name"].casefold():
                name_mismatch.append((report["report_id"], observation["test_name"], code))
            if observation["unit"] not in known_units:
                unknown_unit.append((report["report_id"], observation["unit"], code))
            assert values_equal(observation["printed_value_text"], observation["value"])
            assert observation["reference_range_text"]
    assert unknown_loinc == []
    assert unknown_unit == []
    assert name_mismatch == []

    # Coverage: the dataset must exercise both LOINC paths, duplicates and units.
    printed = [o for report in REPORTS for o in report["expected_observations"]
               if o["loinc_printed"]]
    assert 0 < len(printed) < EXPECTED_OBSERVATIONS
    names = [o["test_name"] for report in REPORTS for o in report["expected_observations"]]
    assert len(names) > len(set(names)), "dataset must contain a repeated test name"
    assert len({o["unit"] for report in REPORTS
                for o in report["expected_observations"]}) >= 5
    print(f"  PASS: {len(supported)} supported labs only, "
          f"{len(set(names))} distinct test names, "
          f"{len({o['unit'] for report in REPORTS for o in report['expected_observations']})} units, "
          f"{len(printed)} printed LOINC columns")


def test_committed_report_pdfs_are_byte_reproducible(tmp_path):
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest_hashes = {entry["report_id"]: entry["sha256"]
                       for entry in manifest["reports"]}

    generated = pdf_fixtures.generate_all(GROUND_TRUTH, tmp_path)
    assert set(generated) == set(manifest_hashes)
    for report_id, generated_path in generated.items():
        fresh = pdf_fixtures.sha256_of(generated_path)
        committed = pdf_fixtures.sha256_of(reports_dir() / f"{report_id}.pdf")
        assert fresh == committed == manifest_hashes[report_id], report_id

    # A second generation is byte-identical as well (no timestamps/randomness).
    second_dir = tmp_path / "second"
    pdf_fixtures.generate_all(GROUND_TRUTH, second_dir)
    for report_id in manifest_hashes:
        assert pdf_fixtures.sha256_of(second_dir / f"{report_id}.pdf") == \
            manifest_hashes[report_id]
    print(f"  PASS: {len(generated)} committed report PDFs are byte-reproducible")


def test_extracted_observations_still_feed_the_reference_range_lookup():
    run = extract_report_observations(
        reports_dir() / "synthea_cmp_0001.pdf", report_id="synthea_cmp_0001")
    checked = lookup_ranges(run.observations)

    assert len(checked) == len(run.observations)
    for extracted, resolved in zip(run.observations, checked):
        assert (resolved.test_name, resolved.loinc_code) == (
            extracted.test_name, extracted.loinc_code)
    glucose = checked[0]
    assert (glucose.test_name, glucose.loinc_code, glucose.value, glucose.unit) == (
        "Glucose", "2345-7", 92.0, "mg/dL")
    assert glucose.range_available is True
    print("  PASS: extracted values feed agents.reference_range.lookup_ranges unchanged")



