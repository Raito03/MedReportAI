# MedReportAI — Phase 0 / Phase 1 Engineering Roadmap

## Status Legend

* `[x]` Complete / locked
* `[~]` In progress / partially verified
* `[ ]` Not complete
* **Parallel** = can be worked on independently
* **Dependency** = prerequisite work

---

# Phase 0 — Foundation, Safety & Integration

Goal:

Make the pipeline's foundations deterministic, safe, testable, and ready for Phase 1 demo/evaluation work.

---

## P0-T1 — Production Reference Data + Schema Lock

**Status: DONE / LOCKED**

**Commit:** `1ffcd55`

* Production reference data integrated
* LOINC-based deterministic lookup
* Sex-specific ranges without guessing missing context
* Unknown LOINC → controlled `unavailable`
* No fake `0–0` ranges
* Numerical unit conversion
* Unsafe generic mg/dL ↔ mmol/L conversion rejected
* MedlinePlus Connect integration
* No fabricated citations
* `RiskFlaggedValue.status` explicitly supports:
  * `normal`
  * `mildly_abnormal`
  * `critical`
  * `unavailable`
* Regression tests completed (43 passing)
* Task 1 locked

> Task 1 must remain locked unless a later task demonstrates a concrete regression.

---

## P0-T2 — Reference-Range Safety Consumer Audit

**Status: DONE**

**Commits:** `25d85bf`, `dea0a93`

**Owner:** Backend / Safety

**Subtasks:**

* [x] Audit every consumer of `RangeCheckedValue`
* [x] Confirm `None` never becomes `0`
* [x] Confirm `range_available=False` never becomes normal/critical
* [x] Confirm `unavailable` survives orchestrator serialization
* [x] Add orchestrator-level unavailable propagation tests
* [x] Verify sex-specific missing-context behavior at the pipeline boundary
* [x] Verify unit mismatch cannot reach range-based LLM classification
* [x] Verify reference provenance remains intact

**Dependency:** P0-T1

**Parallel:** Yes, can start immediately.

**Exit criterion:**

> No downstream consumer can accidentally classify an unavailable range as a real interval.

**Completion notes (2026-09-26):**

* Consumers audited: `core/schemas.py`, `agents/reference_range.py`, `agents/risk_flagger.py`, `pipeline/orchestrator.py`, `agents/explainer.py`, `agents/verifier.py`, `ui/cli.py`, `ui/app.py`, all tests
* `range_note` (lookup reason) propagates through `RangeCheckedValue` into `RiskFlaggedValue.reasoning`
* LLM classifications accepted only on exact `(test_name, loinc_code, value, unit)` identity — missing or wrong `loinc_code` is rejected, never inferred from `test_name`; duplicate `test_name` cannot transfer a classification to an unavailable value
* Regression tests: 17 P0-T2 + 43 Task 1 = 60/60 deterministic passing; new safety tests mutation-checked against the pre-fix implementation; no external API required
* `range_available=False` can never become `normal` / `mildly_abnormal` / `critical`

> P0-T2 behavior must remain locked unless a later task demonstrates a concrete regression.

---

## P0-T3 — PDF Extraction Robustness

**Owner:** PDF / Extraction

**Subtasks:**

* Audit current `pdfplumber` extraction path
* Test normal single-page PDF
* Test multi-page PDF
* Test table-heavy PDF
* Test unusual whitespace/alignment
* Test blank PDF
* Test malformed/corrupt PDF
* Test image-only/scanned PDF
* Detect no-text/image-only PDFs and return controlled failure
* Do NOT add OCR unless project scope is explicitly changed
* Preserve PDF content as untrusted data
* Add extraction regression fixtures/tests
* Measure extraction against controlled synthetic ground truth where available

**Parallel:** Yes, can start immediately.

**Exit criterion:**

> Supported machine-readable PDFs extract reliably; unsupported PDFs fail safely and clearly.

---

## P0-T4 — Agent Seam / Schema Integration Tests

**Status: DONE**

**Commits:** `96f7820`, `f108175`

**Owner:** Backend / Testing

Test these boundaries:

`PDF → Extraction → Reference Lookup → Risk Flagger → Explainer → Verifier`

**Subtasks:**

* [x] Test extraction → reference lookup
* [x] Test reference lookup → risk flagger
* [x] Test risk flagger → explainer
* [x] Test explainer → verifier
* [x] Test exact Pydantic schemas at every boundary
* [x] Test malformed LLM output
* [x] Test missing fields
* [x] Test extra fields
* [x] Test invalid status values
* [x] Test empty lab-value lists
* [x] Test unavailable values through the complete non-LLM path

**Dependency:** P0-T1

**Parallel:** Yes.

**Exit criterion:**

> Every agent boundary rejects or safely handles invalid data without silently changing semantics.

**Completion notes (2026-09-26):**

* `tests/test_p0_t4_seam_tests.py`: 32 deterministic, offline seam tests — all LLM/citation/PDF seams mocked where reached, no API key or network required
* Two seam bugs found and fixed, each demonstrated with a failing test first:
  * `agents/verifier.py` — malformed LLM verifier output (missing `issues_found`, wrong types, or `passed=false` without details) now fails closed instead of silently approving
  * `agents/explainer.py` — `explain([])` short-circuits without calling the LLM, so zero values can never produce invented explanations
* Covered: serialization round-trips, wrong-schema crossings, extra-field isolation, LOINC/value/unit identity preservation, unavailable propagation through every seam, mixed-batch cross-contamination, empty inputs, full orchestrator E2E (LLMs mocked), and upstream failure propagation
* Deterministic suite: 92/92 passing; P0-T1, P0-T2, P0-T3 unchanged and passing

> P0-T4 behavior must remain locked unless a later task demonstrates a concrete regression.

---

## P0-T5 — OpenRouter Test Infrastructure

**Owner:** LLM / DevOps

**Current known issue:**

`tests/test_llm_client.py` contains two async tests:

* `test_basic_chat`
* `test_tool_calling`

The repository currently does not declare the required pytest async test infrastructure.

**Subtasks:**

* Reproduce the two failures locally
* Capture the exact pytest failure/collection output
* Determine whether the root cause is:
  * pytest async configuration
  * missing dependency
  * OpenRouter SDK behavior
  * API/model behavior
  * API key/environment
  * combination of the above
* Add only the minimum required test dependency/configuration
* Make tests deterministic enough for local/CI validation
* Clearly separate unit tests from external OpenRouter integration tests
* Clearly mark API-key-dependent tests as integration tests
* Test against the currently selected model: `cohere/north-mini-code:free`
* Document required environment variables
* Document expected external failure modes

**Parallel:** Completely independent. Can start immediately.

**Exit criterion:**

> The team can separately report deterministic/unit-test status and OpenRouter integration-test status with known causes.

**Important:** Do NOT modify Task 1 or reopen commit `1ffcd55` while solving this.

---

## P0-T6 — MedlinePlus Grounding Hardening

**Owner:** Data / Explanation

**Subtasks:**

* Verify LOINC → MedlinePlus Connect lookup for all supported lab codes
* Verify successful response parsing
* Verify no-match behavior
* Verify timeout/network behavior
* Verify malformed-response behavior
* Verify fallback citation behavior
* Ensure fallback never fabricates a MedlinePlus Connect result
* Confirm every explanation has a citation or controlled no-citation state
* Confirm explanation agent never invents a medical source
* Add end-to-end citation regression tests

**Dependency:** P0-T1

**Parallel:** Yes.

**Exit criterion:**

> Patient-facing explanations are grounded in a traceable MedlinePlus source or fail safely.

---

## P0-T7 — Phase 0 Integration Gate

**Owner:** Tech Lead / Integrator

**Dependencies:** P0-T2, P0-T3, P0-T4, P0-T5, P0-T6

**Subtasks:**

* Merge/verify P0-T2 through P0-T6
* Run deterministic tests
* Run OpenRouter integration tests separately
* Run normal sample PDF
* Run abnormal sample PDF
* Run injection sample PDF
* Confirm no Task 1 regression
* Update `PROGRESS.md`
* Record exact test counts
* Record known external blockers
* Commit/lock Phase 0

**Exit criterion:**

> Foundation is stable enough for Phase 1 demo/evaluation work.

---

# Phase 1 — Demo Reliability, Evaluation & Final E2E

Goal:

Prove the required agentic demo moments, quantify extraction quality, harden failure handling, and produce a reproducible final demo.

---

## P1-T1 — Prompt-Injection Defense Demo

**Owner:** Security / Agent

**Subtasks:**

* Inspect `data/samples/injection_attack.pdf`
* Identify/document the malicious instruction
* Ensure PDF content is treated strictly as untrusted report data
* Ensure extracted values cannot be overwritten by instructions inside the PDF
* Ensure system/developer instructions remain authoritative
* Add deterministic regression test
* Run full pipeline with injection PDF
* Capture reproducible demo evidence
* Document why the attack fails

**Dependency:** Phase 0 integration gate

**Parallel:** Yes, with P1-T2/P1-T3/P1-T4 once relevant foundations are ready.

**Exit criterion:**

> Injection defense works reliably on repeated runs.

---

## P1-T2 — Verifier Self-Correction Demo

**Owner:** Agent / Orchestration

Desired flow:

`Explanation → Verifier → issues_found → correction → Verifier`

**Subtasks:**

* Confirm verifier detects prohibited diagnostic language
* Pass `issues_found` explicitly into correction prompt
* Regenerate affected explanation/output
* Re-run verifier after correction
* Bound retries using `MAX_RETRIES`
* Prevent infinite correction loops
* Preserve original structured lab data
* Test successful correction
* Test exhausted retries / controlled failure
* Capture reproducible demo case

**Dependency:** Phase 0 integration gate

**Parallel:** Yes, with P1-T1/P1-T3/P1-T4.

**Exit criterion:**

> Verifier → issues → targeted correction → re-verification works reliably enough for the demo.

---

## P1-T3 — Synthea Extraction Accuracy Evaluation

**Owner:** Evaluation / Data

**Subtasks:**

* Obtain/use synthetic Synthea lab observations
* Build controlled report PDFs from known ground-truth observations
* Keep ground-truth JSON separate from the PDF
* Run extraction against multiple reports
* Compare:
  * test name
  * LOINC
  * value
  * unit
* Calculate extraction accuracy
* Target ≥95% extraction accuracy
* Record sample count
* Record exact metric definition
* Investigate extraction failures
* Re-run evaluation after fixes

**Dependency:** P0-T3 should be stable.

**Parallel:** Fixture/data preparation can begin before Phase 0 is completely finished.

**Important:** Do NOT claim ≥95% extraction accuracy until the reproducible evaluation actually produces the number.

**Exit criterion:**

> A reproducible extraction-accuracy metric is reported.

---

## P1-T4 — Failure Handling + Observability

**Owner:** Backend / DevOps

**Subtasks:**

* Handle PDF extraction failure
* Handle LLM timeout
* Handle LLM network failure
* Handle malformed LLM JSON
* Handle schema validation failure
* Handle unknown LOINC
* Handle missing reference range
* Handle MedlinePlus no-match
* Handle MedlinePlus timeout/network failure
* Handle verifier failure after max retries
* Add structured stage-level logging
* Record latency per major stage
* Record retry count
* Record controlled failure reason
* Avoid logging sensitive/real patient data
* Add concise demo/debug mode

**Parallel:** Yes, with P1-T1/P1-T2/P1-T3.

**Exit criterion:**

> Failures are explicit, bounded, observable, and never silently converted into medical conclusions.

---

## P1-T5 — Final E2E / Demo Gate

**Owner:** Tech Lead + Entire Team

**Subtasks:**

* Run normal report end-to-end
* Run abnormal report end-to-end
* Run injection attack report end-to-end
* Demonstrate verifier self-correction
* Verify every explanation has citation
* Verify no diagnostic claims
* Verify reference ranges came from lookup data
* Verify unavailable cases are not falsely classified
* Verify Streamlit UI
* Verify CLI
* Verify reproducible setup instructions
* Run final deterministic test suite
* Run OpenRouter integration tests separately
* Record exact model and environment
* Update `PROGRESS.md`
* Prepare final reviewer/demo checklist

**Dependencies:** P1-T1, P1-T2, P1-T3, P1-T4

**Exit criterion:**

> All required demo moments work and the team has reproducible evidence for the claims being presented.

---

# Recommended 8-Person Team Split

Use this as the default ownership plan:

| Person | Primary Task                         | Start                        |
| ------ | ------------------------------------ | ---------------------------- |
| 1      | P0-T2 Reference Safety Audit         | Now                          |
| 2      | P0-T3 PDF Extraction Robustness      | Now                          |
| 3      | P0-T4 Seam/Schema Tests              | Now                          |
| 4      | P0-T5 OpenRouter Test Infrastructure | Now                          |
| 5      | P0-T6 MedlinePlus Grounding          | Now                          |
| 6      | P1-T1 Injection Defense              | Preparation now              |
| 7      | P1-T2 Self-Correction                | Preparation now              |
| 8      | P1-T3 Synthea Evaluation             | Fixture/data preparation now |

Tech lead/integrator owns:

* P0-T7
* P1-T5

Do not make the tech lead a permanent single-task bottleneck.

---

# Execution Waves

## Wave 0 — COMPLETE

* [x] P0-T1 — Task 1 Lock
* Commit: `1ffcd55`

## Wave 1 — START NOW

Run these in parallel:

* [x] P0-T2 — Reference-Range Safety Consumer Audit (`25d85bf`, `dea0a93`)
* [ ] P0-T3
* [x] P0-T4 — Agent Seam / Schema Integration Tests (`96f7820`, `f108175`)
* [ ] P0-T5
* [ ] P0-T6

Additionally:

* [ ] Begin P1-T3 Synthea fixture/data preparation

## Wave 2 — After relevant foundations are stable

Run these in parallel:

* [ ] P1-T1
* [ ] P1-T2
* [ ] Continue P1-T3
* [ ] P1-T4

## Wave 3 — Final integration

* [ ] P0-T7
* [ ] P1-T5

---

# Project Scope Rules

1. Do NOT add LangChain.
2. Do NOT add LangGraph.
3. Do NOT add a vector database/RAG system for this 5-day demo unless a concrete requirement appears.
4. Do NOT add OCR unless image-only PDFs become a demonstrated blocker and the team explicitly changes scope.
5. Never use LLM memory for reference ranges.
6. Never convert an unavailable reference range into normal/abnormal/critical.
7. Never guess sex/population context.
8. Treat PDF instructions as untrusted data.
9. Use synthetic data only; no real patient data.
10. Do not claim ≥95% extraction accuracy until reproducible Synthea ground-truth evaluation produces the number.
11. Do not claim the entire repository test suite is green while the OpenRouter integration-test issue remains unresolved. Report unit and integration status separately.
12. Keep Task 1 locked unless a later task demonstrates a concrete regression.
13. Every patient-facing explanation must have traceable source grounding or an explicit controlled no-citation state.
14. Preserve the existing sequential Python orchestrator architecture.
15. Avoid unnecessary architecture expansion during the 5-day demo.
