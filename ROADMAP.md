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

**Status: DONE**

**Commit:** `68f5b85`

**Owner:** PDF / Extraction

**Subtasks:**

* [x] Audit current `pdfplumber` extraction path
* [x] Test normal single-page PDF
* [x] Test multi-page PDF
* [x] Test table-heavy PDF
* [x] Test unusual whitespace/alignment
* [x] Test blank PDF
* [x] Test malformed/corrupt PDF
* [x] Test image-only/scanned PDF
* [x] Detect no-text/image-only PDFs and return controlled failure
* [x] Do NOT add OCR unless project scope is explicitly changed
* [x] Preserve PDF content as untrusted data
* [x] Add extraction regression fixtures/tests
* [x] Measure extraction against controlled synthetic ground truth where available

**Parallel:** Yes, can start immediately.

**Exit criterion:**

> Supported machine-readable PDFs extract reliably; unsupported PDFs fail safely and clearly.

**Completion notes (2026-09-26):**

* `tools/pdf_extractor.py`: `ValueError`-based controlled failure hierarchy — `PdfExtractionError` → `PdfInvalidError` (corrupt/truncated/empty/not-a-PDF) and `PdfNoTextError(reason="blank" | "image_only")`; `pdf_to_text(file_path) -> str` signature and success behavior unchanged; existing callers and orchestrator safety net untouched
* Image-only/scanned PDFs report an explicit "unsupported (OCR is out of scope)" failure — no OCR introduced (guarded by `test_extractor_contains_no_ocr_dependencies`)
* `tests/test_pdf_extraction.py`: 12 deterministic, offline tests covering all seven required PDF categories; all fixtures generated programmatically at test time (reportlab, a minimal stdlib-built image-only PDF, deterministic corrupt/truncated/empty bytes) — no binary fixtures, no API key, no network
* `ui/cli.py` / `ui/app.py`: controlled extraction-error messages instead of tracebacks
* Merged deterministic suite after integration: **104 passed, 2 skipped** (the 2 skips are the pre-existing `test_llm_client.py` async OpenRouter integration tests — P0-T5)

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

**Status: DONE**

**Commit:** `a89bf84`

**Owner:** LLM / DevOps

**Resolved known issue (was: two failing async tests):**

`tests/test_llm_client.py` contained two async tests:

* `test_basic_chat`
* `test_tool_calling`

**Root cause (determined):** the tests were `async def` with no pytest async
plugin declared, and — more fundamentally — real-API integration tests were
running inside the normal deterministic suite. **Resolution:** no new test
dependency was added; the tests were converted to sync wrappers and gated as
Mode B integration tests (see below).

**Subtasks:**

* [x] Reproduce the two failures locally
* [x] Capture the exact pytest failure/collection output
* [x] Determine whether the root cause is:
  * pytest async configuration
  * missing dependency
  * OpenRouter SDK behavior
  * API/model behavior
  * API key/environment
  * combination of the above
* [x] Add only the minimum required test dependency/configuration
* [x] Make tests deterministic enough for local/CI validation
* [x] Clearly separate unit tests from external OpenRouter integration tests
* [x] Clearly mark API-key-dependent tests as integration tests
* [x] Test against the currently selected model: `cohere/north-mini-code:free`
* [x] Document required environment variables
* [x] Document expected external failure modes

**Parallel:** Completely independent. Can start immediately.

**Exit criterion:**

> The team can separately report deterministic/unit-test status and OpenRouter integration-test status with known causes.

**Important:** Do NOT modify Task 1 or reopen commit `1ffcd55` while solving this.

**Completion notes (2026-09-26):**

* Two test modes, clearly separated:
  * **Mode A (deterministic/offline):** normal `pytest` — never calls OpenRouter, never needs a key. An autouse guard (`tests/conftest.py`) fails any unexpected agent LLM call immediately.
  * **Mode B (integration):** `tests/test_llm_client.py` + `tests/integration/test_openrouter_smoke.py` — gated behind `OPENROUTER_RUN_INTEGRATION=1`, skip with `SKIPPED: OPENROUTER_API_KEY is not configured` when no key; never run accidentally
* Reusable `FakeLLM` (`tests/fake_llm.py`): sequenced deterministic responses, full call recording (model/input/tools/stop_when), `call_count`/`last_prompt`, exception simulation, controlled failure on unexpected extra calls
* 38 deterministic tests cover: response parsing (valid/empty/malformed/missing-content), timeout/connection/401/403/429/500/503/408 via the SDK's real `openrouter.errors` types (single attempt, no invented retry), API-key leak protection (repo-wide credential scan + `.gitignore` rules), model configuration/override, all four LLM agents driven by the fake, cross-stage response sequencing, malformed LLM output
* Live verification against `cohere/north-mini-code:free`: smoke + connectivity tests **3 passed**
* Full suite: **142 passed, 3 skipped, 0 failed**; deterministic and integration status now reportable separately (Scope Rule 11 satisfied)
* `.gitignore` hardened: `.env` + `.env.*` ignored, `.env.example` stays tracked; no credentials committed

> P0-T5 behavior must remain locked unless a later task demonstrates a concrete regression.

---

## P0-T6 — MedlinePlus Grounding Hardening

**Status: DONE**

**Commit:** `c1b8e42`

**Owner:** Data / Explanation

**Subtasks:**

* [x] Verify LOINC → MedlinePlus Connect lookup for all supported lab codes
* [x] Verify successful response parsing
* [x] Verify no-match behavior
* [x] Verify timeout/network behavior
* [x] Verify malformed-response behavior
* [x] Verify fallback citation behavior
* [x] Ensure fallback never fabricates a MedlinePlus Connect result
* [x] Confirm every explanation has a citation or controlled no-citation state
* [x] Confirm explanation agent never invents a medical source
* [x] Add end-to-end citation regression tests

**Dependency:** P0-T1

**Parallel:** Yes.

**Exit criterion:**

> Patient-facing explanations are grounded in a traceable MedlinePlus source or fail safely.

**Completion notes (2026-09-26):**

* `is_trusted_medlineplus_url()` trust policy: only `medlineplus.gov` (exact/subdomain), http(s), no credentials — suffix/path/userinfo/scheme tricks rejected; enforced on Connect responses, fallback pages, and derived citation fields
* Response parsing hardened for all project-expected shapes (`feed`/`entries`/`result`, single record, `[{"url": ...}]`); `None/{}/[{}]/[{"foo":"bar"}]/42`/invalid JSON fail controlled with debuggable errors; `URLError(timeout)` classified as timeout
* `agents/explainer.py` builds a trusted citation map from the lookup layer and **always overrides** the LLM's citation; LLM-echoed `citation_url`/`citation_status` are stripped; an invented `test_name` receives the explicit no-citation state — the LLM can never invent or change a source
* `FinalExplanation` extended (not replaced) with `citation_url: Optional[str]` and `citation_status: Literal["available","unavailable"]` — explicit controlled no-citation state, backward-compatible defaults
* `tests/test_p0_t6_citation_grounding.py`: 16 deterministic tests (HTTP + LLM fully mocked) covering all 12 required behavior cases, all 27 mapped LOINC codes, and a full mocked orchestrator E2E; `tests/test_medlineplus_live.py`: gated live test (`MEDLINEPLUS_LIVE=1`)
* Exact results: P0-T6 16/16; full suite after rebase onto the concurrent P0-T5 test infra (`a89bf84`) = **158 passed, 4 skipped** (all 4 skips are gated external tests: 3 OpenRouter per P0-T5, 1 gated live MedlinePlus); P0-T6 alone was 120 passed / 3 skipped; P0-T1..T4 regression 104/104; live MedlinePlus test **PASSED** (0.84s)

---

## P0-T7 — Phase 0 Integration Gate

**Status: DONE / LOCKED**

**Commit:** `77f5130`

**Owner:** Tech Lead / Integrator

**Dependencies:** P0-T2, P0-T3, P0-T4, P0-T5, P0-T6

**Subtasks:**

* [x] Merge/verify P0-T2 through P0-T6
* [x] Run deterministic tests (168 passed, 4 skipped)
* [x] Run OpenRouter integration tests separately (3 passed)
* [x] Run normal sample PDF (extraction verified)
* [x] Run abnormal sample PDF (extraction verified)
* [x] Run injection sample PDF (extraction verified)
* [x] Confirm no Task 1 regression
* [x] Update `PROGRESS.md`
* [x] Record exact test counts
* [x] Record known external blockers
* [x] Commit/lock Phase 0

**Exit criterion:**

> Foundation is stable enough for Phase 1 demo/evaluation work.

**Completion notes (2026-09-26):**

* Full deterministic test suite: **168 passed, 4 skipped** (skips are gated external tests: OpenRouter integration + MedlinePlus live)
* OpenRouter integration tests: **3 passed** (basic_chat, tool_calling, structured_output)
* Sample PDFs verified: all three PDFs extract correctly (normal: 552 chars, abnormal: 555 chars, injection: 690 chars)
* Task 1 regression verified: no changes to locked P0-T1 contracts
* P0-T7 integration gate test file: `tests/test_p0_t7_integration_gate.py` (comprehensive E2E with FakeLLM + controlled MedlinePlus boundary)
* Phase 0 is now locked for Phase 1 demo/evaluation work

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

**Status: DONE**

**Owner:** Backend / DevOps

**Subtasks:**

* [x] Handle PDF extraction failure
* [x] Handle LLM timeout
* [x] Handle LLM network failure
* [x] Handle malformed LLM JSON
* [x] Handle schema validation failure
* [x] Handle unknown LOINC
* [x] Handle missing reference range
* [x] Handle MedlinePlus no-match
* [x] Handle MedlinePlus timeout/network failure
* [x] Handle verifier failure after max retries
* [x] Add structured stage-level logging
* [x] Record latency per major stage
* [x] Record retry count
* [x] Record controlled failure reason
* [x] Avoid logging sensitive/real patient data
* [x] Add concise demo/debug mode

**Parallel:** Yes, with P1-T1/P1-T2/P1-T3.

**Exit criterion:**

> Failures are explicit, bounded, observable, and never silently converted into medical conclusions.

**Completion notes:**

* 54 deterministic tests covering all failure categories
* Failure taxonomy: 15 explicit failure types (pdf_error, llm_timeout, llm_network_error, etc.)
* Observability: PipelineLogger, LatencyTracker, PipelineEvent structured events
* Privacy: contains_sensitive_data(), sanitize_metadata() protect patient data
* Full suite: 222 passed, 4 skipped (gated external tests only)
* No P0 regressions

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
* [x] P0-T3 — PDF Extraction Robustness (`68f5b85`)
* [x] P0-T4 — Agent Seam / Schema Integration Tests (`96f7820`, `f108175`)
* [x] P0-T5 — OpenRouter Test Infrastructure (`a89bf84`)
* [x] P0-T6 — MedlinePlus Grounding Hardening (`c1b8e42`)

Additionally:

* [ ] Begin P1-T3 Synthea fixture/data preparation

## Wave 2 — After relevant foundations are stable

Run these in parallel:

* [ ] P1-T1
* [ ] P1-T2
* [ ] Continue P1-T3
* [x] P1-T4 — Failure Handling + Observability

## Wave 3 — Final integration

* [x] P0-T7 — Phase 0 Integration Gate (`77f5130`)
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
