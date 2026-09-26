# Progress Log — Lab Report Explainer Agent

## Current Status
Task 1 LOCKED. Schema contract fixed (`unavailable` explicitly supported via `Literal`). 43 unit tests pass (24 reference range, 9 schema, 10 MedlinePlus). Unit conversion, unavailable-range propagation, and risk flagger guard all verified. E2E non-LLM path verified end-to-end.

**P0-T3 (PDF extraction robustness) completed 2026-09-26** - controlled failures for blank/corrupt/image-only PDFs, no OCR; 12/12 P0-T3 tests pass; merged deterministic suite (P0-T1..P0-T4): 104 passed / 2 skipped (pre-existing OpenRouter async integration skips). See the "P0-T3" section below.
**P0-T6 (MedlinePlus grounding hardening) completed 2026-09-26** - trusted-URL policy, hardened response parsing, timeout/network/malformed controlled failures, LLM can no longer invent or change a citation, explicit `citation_url`/`citation_status` on `FinalExplanation`; full suite 158 passed / 4 skipped (all skips are gated external tests: 3 OpenRouter per P0-T5 + 1 gated live MedlinePlus); live MedlinePlus test PASSED. See the "P0-T6" section below.

---

## Latest Fixes (2026-09-26 — post-Task 1)

### LOINC Resolution from Test Names
**Problem:** Extraction agent returned `loinc_code: "unknown"` for all values because sample PDFs don't contain LOINC codes. This cascaded: unknown LOINC → reference lookup failed → 0/0 ranges → risk flagger classified everything as "critical".

**Fix:** Added post-processing in `agents/extraction.py` — resolves LOINC codes from test names via `supported_labs.json` when the LLM returns "unknown".

### Unit Equivalences
**Problem:** Common abbreviations (`K/uL`, `M/uL`) didn't match reference unit names (`cells/mcL`, `million cells/mcL`), causing all lookups to fail with "unit mismatch".

**Fix:** Added bidirectional unit equivalence mapping in `agents/reference_range.py`:
- `K/uL` ↔ `cells/mcL` ↔ `cells/µL` ↔ `10^3/uL`
- `M/uL` ↔ `million cells/mcL` ↔ `million/uL` ↔ `10^6/uL`
- `pg/cell` ↔ `pg`
- `%` ↔ `percent`

### MedlinePlus URL Parsing
**Problem:** MedlinePlus Connect returns `link` as a list of dicts (`[{"href": "...", "rel": "alternate"}]`), not a string or single dict. Old parser only handled dict/string.

**Fix:** Updated `tools/medlineplus_connect.py` to handle list-of-dicts format, extracting `href` from the first entry.

### Risk Flagger Prompt
**Problem:** LLM misclassified all values as "outside range" despite correct reference data.

**Fix:** Updated prompt with explicit comparison instructions and an example (Glucose 92 between 70-100 → normal).

### Pipeline Results (Normal Report — 2026-09-26)
```
[1/5] PDF extraction:      552 chars ✅
[2/5] Lab extraction:      14 values ✅ (LOINC resolved from test names)
[3/5] Reference lookup:    14 range-checked ✅ (units now match)
[4/5] Risk classification: 14 classified ✅
[5/5] Explanations:        MedlinePlus citations working ✅
      Verifier caught missing WBC/RBC citations ✅
      Self-correction attempted ✅
```

**Citations now working:**
- "MedlinePlus: Blood Glucose (https://medlineplus.gov/bloodglucose.html?...)"
- "MedlinePlus: Blood Count Tests (https://medlineplus.gov/bloodcounttests.html?...)"
- "MedlinePlus: Creatinine Test (https://medlineplus.gov/lab-tests/creatinine-test?...)"
- "MedlinePlus: Cholesterol (https://medlineplus.gov/cholesterol.html?...)"

---

## Task 1 Hardening (2026-09-26)

### Fix 1 — Unit Conversion (numerical, not just textual)

**Problem:** `K/uL` and `cells/mcL` were treated as "compatible" without numerical conversion. `7 K/uL` was compared directly against `4500–11000 cells/mcL`, producing incorrect abnormal results.

**Fix:** Implemented `_get_conversion_factor()` and `_convert_value()` with a deterministic conversion table. Values are now numerically converted BEFORE reference-range comparison.

**Conversions implemented:**
- `K/uL` → `cells/mcL`: multiply by 1000
- `10^3/uL` → `cells/mcL`: multiply by 1000
- `cells/mcL` → `K/uL`: multiply by 0.001
- `M/uL` ↔ `million cells/mcL`: same scale (factor 1.0)
- `%` ↔ `percent`: same scale (factor 1.0)
- `pg/cell` ↔ `pg`: same scale (factor 1.0)
- `mg/dL` ↔ `mmol/L`: NOT supported (analyte-specific, returns controlled failure)

**Example:** `7 K/uL → 7000 cells/mcL → in_range = true (4500–11000)`

**Safety:** No generic mg/dL ↔ mmol/L conversion. Unit mismatch returns controlled failure.

### Fix 2 — Unavailable Reference Range Propagation

**Problem:** `lookup_ranges()` converted unavailable references to `reference_low=0, reference_high=0`. Downstream agents could interpret `0–0` as a real reference interval and classify values as critically abnormal.

**Fix:**
- `RangeCheckedValue` schema: `reference_low` and `reference_high` changed from `float` to `Optional[float]` (default `None`)
- Added `range_available: bool` field to `RangeCheckedValue`
- `lookup_ranges()` now preserves `None` for unavailable ranges instead of converting to `0.0`
- Risk flagger guards against unavailable ranges: values with `range_available=False` are NOT sent to the LLM for range-based classification. They get `status="unavailable"` with a descriptive reasoning.

**Three distinguishable states:**
- Case A (Unknown LOINC): `range_available=False`, note="not in supported labs list"
- Case B (Known LOINC, no applicable range): `range_available=False`, note="sex-specific ranges available but patient sex not provided"
- Case C (Known LOINC + valid range): `range_available=True`, normal comparison

### Test Results (post-hardening)

```
tests/test_reference_range.py 24 passed  (was 15, added 9 unit conversion tests)
tests/test_schemas.py          6 passed
tests/test_medlineplus.py     10 passed
Total: 40 passed in 2.15s
```

### E2E Results (normal_report.pdf)

```
[1/5] PDF extraction:      552 chars ✅
[2/5] Lab extraction:      14 values ✅
[3/5] Reference lookup:    14 range-checked ✅ (unit conversion working)
[4/5] Risk classification: 14 classified ✅ (unavailable ranges guarded)
[5/5] Explanations:        Rate-limited on retry (external constraint)
```

**Injection defense and self-correction demos not yet tested** — blocked by rate limit on free tier.

---

## Task 1 Lock (2026-09-26)

### Schema Contract Fix

**Problem:** `RiskFlaggedValue.status` was documented as `str  # "normal" | "mildly_abnormal" | "critical"` but the risk flagger legitimately returns `"unavailable"` when no valid reference range exists.

**Fix:** Changed `status` field to `Literal["normal", "mildly_abnormal", "critical", "unavailable"]`. The schema now explicitly permits exactly the statuses the implementation can return.

**Docstring clarification:** `unavailable` means "No valid applicable reference range was available, so no range-based risk classification was performed." It does NOT mean the value is abnormal or critical.

### Verification Results

- **Unknown LOINC** → `reference_low=None, reference_high=None, range_available=False` → `status="unavailable"` ✓
- **Missing population context** → same unavailable path ✓
- **Unit mismatch (mmol/L vs mg/dL)** → `range_available=False`, no generic conversion ✓
- **Unit conversion (K/uL → cells/mcL)** → 7 K/uL → 7000 cells/mcL → in_range=True ✓
- **Risk flagger guard** → unavailable values NOT sent to LLM for range classification ✓
- **Schema validation** → `Literal` rejects invalid status values (e.g. "severe") ✓
- **MedlinePlus integration** → unchanged, all behaviors preserved ✓

### Test Results (post-lock)

```
tests/test_reference_range.py 24 passed
tests/test_schemas.py          9 passed  (was 6, added 3 for unavailable status)
tests/test_medlineplus.py     10 passed
Total: 43 passed in 1.94s
```

(2 pre-existing async failures in test_llm_client.py — unrelated to Task 1)

---

## Task 1 Completion Summary (2026-09-26)

### What Changed

| File | Change |
|------|--------|
| `agents/reference_range.py` | Rewritten: nested `ranges_by_loinc` structure, sex-specific ranges, unit equivalences, unknown LOINC → controlled failure, `lookup_range_detail()` returns full metadata |
| `agents/extraction.py` | Updated: post-processes LOINC codes from test names via `supported_labs.json` |
| `agents/explainer.py` | Updated: citations sourced from MedlinePlus Connect via `get_citation()`, fabricated citations replaced |
| `agents/risk_flagger.py` | Updated: prompt includes `loinc_code` in output, explicit range comparison instructions |
| `core/schemas.py` | Updated: `RiskFlaggedValue` gained `loinc_code: str = ""` field |
| `tools/medlineplus_connect.py` | **NEW**: MedlinePlus Connect client with LOINC OID, response parsing (handles list-of-dicts URLs), timeout/error handling, fallback URLs, no-fabrication guarantee |
| `tests/test_reference_range.py` | Rewritten: 15 tests |
| `tests/test_medlineplus.py` | **NEW**: 10 tests with mocked HTTP |
| `PROGRESS.md` | Updated |

### Data Integration

- **`data/reference_ranges.json`**: Production reference data (MedlinePlus-documented intervals). Nested `ranges_by_loinc` with arrays per LOINC code. Sex-specific ranges for Hemoglobin, Hematocrit, RBC, HDL. **NOT NHANES-derived — MedlinePlus intervals.**
- **`data/supported_labs.json`**: 27 supported lab tests with LOINC codes, canonical units, MedlinePlus Connect URLs, fallback pages.
- **`data/medlineplus_mapping.json`**: LOINC-to-MedlinePlus Connect mapping with runtime URLs and fallback pages.

### Reference Lookup Behavior

| Scenario | Old Behavior | New Behavior |
|----------|-------------|-------------|
| Known LOINC, single range | ✅ Works | ✅ Works |
| Known LOINC, sex-specific ranges | Guessed or returned 0/0 | Controlled failure: "sex-specific ranges available but patient sex not provided" |
| Unknown LOINC | `0, 0, in_range=True` ← UNSAFE | `range_available=False, in_range=False` with note |
| Unit mismatch | Silent comparison | Controlled failure with "Unit mismatch" note |
| Unit abbreviation (K/uL vs cells/mcL) | Mismatch | Recognized as equivalent via mapping |
| Boundary values (70, 100) | Inclusive | Inclusive (verified) |

### MedlinePlus Connect Integration

- **Location**: `tools/medlineplus_connect.py`
- **LOINC OID**: `2.16.840.1.113883.6.1` (used in all requests)
- **Response parsing**: Handles string, dict, and list-of-dicts formats for title/link/summary
- **Timeout**: 10s, returns error + fallback URL
- **Network error**: Returns error + fallback URL
- **Malformed response**: Returns error + fallback URL
- **Citations**: Never fabricated — "no MedlinePlus citation available" when no result
- **Fallback**: Uses `known_human_readable_fallback_page` from mapping

### Test Results

```
tests/test_schemas.py          9 passed  (includes unavailable status tests)
tests/test_reference_range.py 24 passed
tests/test_medlineplus.py     10 passed
Total: 43 passed in 1.94s
```

### Remaining NHANES Work

The current `reference_ranges.json` uses **MedlinePlus-documented intervals**, not NHANES-derived intervals. Per the task instructions, NHANES-specific provenance is a future hardening step.

---

## P0-T3 — PDF Extraction Robustness (2026-09-26)

**Status: COMPLETE** — all acceptance criteria verified by the test results below.

> Note: this P0-T3 work started from the pre-roadmap baseline (`c88c7a5`), before
> `ROADMAP.md` existed locally (the roadmap landed on `origin/master` in `3723f63`).
> The section below has been verified against ROADMAP.md's P0-T3 subtasks and exit
> criterion, and integrated with the P0-T1/P0-T2/P0-T4 work already on `origin/master`,
> which P0-T3 does not modify.

### What changed

| File | Change |
|------|--------|
| `tools/pdf_extractor.py` | Added a small controlled-failure hierarchy: `PdfExtractionError(ValueError)` → `PdfInvalidError` (corrupt/unreadable/not-a-PDF), `PdfNoTextError(reason="blank" \| "image_only")` (readable PDF but no extractable text). All pdfplumber/pdfminer exceptions are wrapped into these; whitespace-only pages are now detected as no-text; image-only detection via `page.images`. `pdf_to_text(file_path) -> str` signature and success behavior unchanged. |
| `ui/cli.py` | Catches `PdfExtractionError` → prints a clear one-line error and exits 1 instead of dumping a traceback. |
| `ui/app.py` | Same controlled handling in the Streamlit path, with temp-file cleanup on failure. |
| `tests/test_pdf_extraction.py` | **New** — 12 deterministic tests (all 7 P0-T3 categories + 2 regression + 1 orchestrator integration + OCR guard + missing-file). |
| `README.md` | Added the new test file to the project-structure listing. |
| `PROGRESS.md` | This section. |

**Not changed by P0-T3 (deliberately):** `pipeline/orchestrator.py` (its `ValueError("PDF
extraction returned empty text")` safety net still stands; the new errors subclass `ValueError`,
so all existing callers keep working), `core/schemas.py`, `agents/*` (LOINC / reference-range /
risk-classification behavior belongs to P0-T1/P0-T2 and was not modified here),
`tools/generate_samples.py`, `data/*`.
No new dependencies were added to `requirements.txt`. No OCR, LangChain, LangGraph, RAG,
or vector store was introduced — architecture remains the sequential Python pipeline.

### Failure classification (project convention = `ValueError`-based)

| Category | Result |
|----------|--------|
| Supported text PDF (incl. multi-page, table-heavy, odd whitespace) | returns `str` (pages joined with `\n\n`, order preserved, blank pages skipped without losing others) |
| Valid PDF, no extractable text, no images (blank) | `PdfNoTextError(reason="blank")` |
| Valid PDF, image only / scanned (no text, has images) | `PdfNoTextError(reason="image_only")` — message explicitly states OCR is unsupported/out of scope |
| Corrupt / truncated / empty / not-a-PDF | `PdfInvalidError` |
| Page read failure after open | `PdfExtractionError` |
| Missing file | `FileNotFoundError` (pre-existing behavior, unchanged) |

### Fixtures added (all generated programmatically at test time — no binary files committed)

- normal text PDF (reportlab) — 1 page, 3 lab rows
- multi-page PDF — 4 pages: text / text / **blank** / text (proves blank middle page loses nothing)
- table-heavy PDF — header + grid rules + 12 realistic lab rows (names, values, units, ranges)
- unusual whitespace PDF — multiple spaces, literal tab characters, tab-stop column jumps, irregular line breaks, side-by-side columns
- blank PDF — one empty page
- image-only PDF — minimal hand-built PDF (stdlib `zlib`): 8×8 embedded image XObject, zero text operators
- corrupt PDFs — 3 deterministic variants: static malformed bytes, valid PDF truncated to 1/3, zero-byte file

### Exact test results (ran on this machine, Python 3.11.3 / pytest 7.3.1)

```text
$ python -m pytest tests/test_pdf_extraction.py -v
12 passed in 2.62s

$ python tests/test_pdf_extraction.py          # __main__ entry point
12 passed in 2.42s   (EXIT=0)

$ python -m pytest tests/ -v                   # merged suite after rebase onto origin/master
                                              # (P0-T1 + P0-T2 + P0-T4 + P0-T3 + MedlinePlus)
104 passed, 2 skipped, 2 warnings in 5.18s   (EXIT=0)
```

CLI controlled-failure checks (clear message + exit code 1, no traceback):

```text
$ python -m ui.cli corrupt.pdf
Error: PDF extraction failed: Cannot read PDF — file is corrupt or not a valid PDF ... (PdfminerException: Unexpected EOF)

$ python -m ui.cli blank.pdf
Error: PDF extraction failed: No extractable text found — the PDF is blank or empty: ...

$ python -m ui.cli image_only.pdf
Error: PDF extraction failed: No extractable text found — the PDF appears to be an image-only/scanned document.
Text extraction without OCR is unsupported (OCR is out of scope): ...
```

### Unrelated failures / blockers (NOT caused by P0-T3)

1. **`tests/test_llm_client.py` — 2 skipped under pytest** (pre-existing): the async tests are
   skipped because no async pytest plugin is installed; the file is designed to run as a
   script and requires an OpenRouter API key + network. On this machine `.env` is absent
   (`API key set: NO`), so its `__main__` run reports both checks as FAIL-by-skip without
   making any network call. Untouched by P0-T3.
2. **`python tests/test_schemas.py` / `test_reference_range.py` as standalone scripts** fail
   with `ModuleNotFoundError: No module named 'core'/'agents'` — **pre-existing**: those
   files have no `sys.path` bootstrap (unlike `test_llm_client.py`) and are run via pytest,
   where they pass (included in the 104 above). Not modified (locked P0-T1/T2 files).

### Environment notes (this machine only — no repo changes)

- No `python` on PATH; used `C:\Users\Ayush\anaconda3\python.exe` (3.11.3, pytest 7.3.1).
- Installed into the env (all already listed in `requirements.txt`, none new):
  `pdfplumber 0.11.10`, `reportlab 5.0.0.1`, `python-dotenv`, `openrouter-agent-sdk 0.8.0`.
- Re-applied the documented `OutputImage` patch to
  `...\site-packages\openrouter_agent\__init__.py` (Known Issue #1 — site-packages patch lost
  on reinstall; without it `import openrouter_agent` fails and even the P0-T2 tests cannot
  import). Patch: removed the nonexistent `OutputImage` from the `openrouter.components`
  import list and aliased `OutputImage = InputImage` before its `__all__` export.

### Limitations

- **OCR remains out of scope and was not introduced** — enforced by
  `test_extractor_contains_no_ocr_dependencies` (guards the extractor source against
  tesseract/easyocr/paddleocr/keras_ocr/ocrmypdf). Image-only PDFs fail with an explicit
  "unsupported (OCR is out of scope)" error instead of fabricated text.
- Table handling = pdfplumber plain-text line extraction (each table row extracts as a
  single line, e.g. `Glucose 145 mg/dL 70-100`). No table-structure reconstruction — not
  required by the existing architecture; no name/value content is silently discarded.
- Literal tab bytes (0x09) inside PDF text strings are mapped by pdfminer through the font
  encoding and can surface as arbitrary glyphs (e.g. `Hemoglobinn14.2ng/dL`). Extraction
  does not crash and every fragment remains present (tested); real tab layouts (column
  jumps) extract fully paired values (tested).
- Blank vs. image-only is distinguished via `page.images`; an exotic scanned PDF could be
  reported as "blank" instead of "image_only", but either way it is a controlled
  `PdfNoTextError` — never a fake success.

---

## P0-T6 — MedlinePlus Grounding Hardening (2026-09-26)

**Status: DONE** — all acceptance criteria verified by the test results below.

### What was hardened

| File | Change |
|------|--------|
| `tools/medlineplus_connect.py` | URL trust policy: `is_trusted_medlineplus_url()` accepts only `medlineplus.gov` (exact or subdomain) over http(s) with no embedded credentials — rejects suffix/path/userinfo/scheme tricks. Response parsing hardened: accepts the original `feed`/`entries`/`result` containers, a single record dict, and plain list-of-dicts (`[{"url": "..."}]`); `None/{}/[{}]/[{"foo":"bar"}]/42` and invalid JSON all fail controlled with a debuggable `result.error`. Untrusted response URLs are never passed through. The mapping's fallback URL is re-validated (defense in depth). `URLError(socket.timeout)` now classifies as `Timeout after Ns`. `get_citation()` priority: validated Connect result → validated deterministic fallback page → explicit `"{test} — no MedlinePlus citation available"` (format unchanged — existing tests rely on it). New helpers: `CITATION_UNAVAILABLE_MARKER`, `derive_citation_fields()`. |
| `agents/explainer.py` | Builds a **trusted citation map** (keyed by `test_name`, sourced from `get_citation()` per LOINC) *before* the LLM call and passes `citation` + `citation_url` + `citation_status` into the prompt. Post-process **always overrides** the LLM's citation with the trusted map — the LLM's citation text, URL, and any echoed grounding fields are discarded; a `test_name` the LLM invented (not looked up) receives the explicit no-citation state. Prompt tightened: do not invent/change URLs; repeat the provided no-citation text when `citation_status` is `unavailable`. |
| `core/schemas.py` | `FinalExplanation` **extended** (no competing schema): `citation_url: Optional[str] = None`, `citation_status: Literal["available", "unavailable"] = "unavailable"`. Defaults keep every existing construction/round-trip valid. |

**Not changed:** `agents/reference_range.py`, `data/*` (reference ranges), `agents/verifier.py`, `tools/pdf_extractor.py`, `pipeline/orchestrator.py`, P0-T1/T2/T4 contracts. No new dependencies; no OCR/LangChain/LangGraph/RAG/vector DB.

### Tests added

* `tests/test_p0_t6_citation_grounding.py` — **16 deterministic tests**; every HTTP call mocked at `urllib.request.urlopen`, every LLM mocked at `call_model`. Covers the 12 required cases: success (list + feed shapes), multiple records, empty response, malformed responses, untrusted URL, timeout, network error, unexpected exception, unknown LOINC, citation propagation (input AND output), no-citation propagation, citation isolation (Glucose `2345-7` vs WBC `6690-2`, LLM citations deliberately swapped), plus URL trust-policy unit checks, an LLM-invented-test_name check, a data-driven controlled-citation check across **all 27 mapped LOINC codes**, a `lookup → risk → explain` integration flow, and a full mocked orchestrator E2E (PDF → verify) asserting the serialized output carries only the validated citation.
* `tests/test_medlineplus_live.py` — live test for known LOINC `2345-7`, gated behind `MEDLINEPLUS_LIVE=1` (skipped by default so the normal suite never depends on live MedlinePlus); unreachable service reports as blocked via skip, never as passed.

### Exact test results

```text
$ python -m pytest tests/test_p0_t6_citation_grounding.py -v
16 passed in 3.01s

$ python -m pytest tests/ -q                    # full suite (P0-T6 + concurrent P0-T5 test infra)
158 passed, 4 skipped in 5.34s                  (EXIT=0)
# skips = 3 x gated OpenRouter integration (P0-T5: OPENROUTER_RUN_INTEGRATION=1)
#       + 1 x gated live MedlinePlus (MEDLINEPLUS_LIVE=1)

$ python -m pytest tests/test_schemas.py tests/test_reference_range.py \
      tests/test_p0_t2_safety.py tests/test_pdf_extraction.py \
      tests/test_p0_t4_seam_tests.py tests/test_medlineplus.py -q   # P0-T1..T4 regression
104 passed in 2.79s                             (EXIT=0)

$ MEDLINEPLUS_LIVE=1 python -m pytest tests/test_medlineplus_live.py -v
1 passed in 0.84s                               (live PASS)
```

Baseline before P0-T6: 104 passed, 2 skipped. P0-T6 alone added 16 tests → 120 passed, 3 skipped. After rebasing onto the concurrently-landed P0-T5 test infrastructure (`a89bf84`, `daeee43`): **158 passed, 4 skipped** — every skip is an intentionally gated external test.

### Limitations

* `FinalExplanation` identifies results by `test_name`, so two results sharing a `test_name` share the first lookup's citation (pre-existing schema identity limitation — no cross-LOINC leakage possible for distinct names, which is tested).
* A response carrying a real title but no parseable trusted URL yields a title-only citation (`citation_url=None`, status `available`).
* The live test and the pre-existing `test_reference_range.py::test_medlineplus_get_citation_known_test` contact `connect.medlineplus.gov` only when run outside the mocked suite paths; the live one reports blocked (skip) if unreachable.

---

## P0-T7 — Phase 0 Integration Gate (2026-09-26)

**Status: DONE / LOCKED** — all acceptance criteria verified by the test results below.

### What was verified

* Full deterministic test suite: **168 passed, 4 skipped** (skips are gated external tests: OpenRouter integration + MedlinePlus live)
* OpenRouter integration tests: **3 passed** (basic_chat, tool_calling, structured_output)
* Sample PDFs verified: all three PDFs extract correctly (normal: 552 chars, abnormal: 555 chars, injection: 690 chars)
* Task 1 regression verified: no changes to locked P0-T1 contracts
* P0-T7 integration gate test file: `tests/test_p0_t7_integration_gate.py` (comprehensive E2E with FakeLLM + controlled MedlinePlus boundary)

### Test results

```text
$ python -m pytest -q
168 passed, 4 skipped in 7.69s                  (EXIT=0)

$ OPENROUTER_RUN_INTEGRATION=1 python -m pytest tests/test_llm_client.py tests/integration/ -q
3 passed in 9.88s                               (EXIT=0)
```

### Sample PDF verification

* `data/samples/normal_report.pdf`: 552 chars extracted, contains Glucose/Creatinine/Hemoglobin values
* `data/samples/abnormal_report.pdf`: 555 chars extracted, contains abnormal values
* `data/samples/injection_attack.pdf`: 690 chars extracted, contains embedded instructions (treated as untrusted data)

### Task 1 regression status

* No changes to locked P0-T1 contracts
* Unknown LOINC → controlled `unavailable` (not `0–0`)
* Unavailable reference ranges remain unavailable
* `unavailable` is not classified as normal/mildly_abnormal/critical
* Sex-specific ranges not guessed
* Unit mismatch remains unavailable
* Supported unit conversions still work
* LLM not used to invent reference ranges
* Risk flagging only classifies values with valid reference ranges

### Phase 0 completion/lock status

Phase 0 Status: **COMPLETE / LOCKED**

P0-T1 through P0-T7 have been implemented and verified. The Phase 0 foundation is now locked for Phase 1 demo/evaluation work.

Future changes should not modify the locked reference-range safety contract or Phase 0 architecture unless a regression or explicitly approved scope change requires it.

---

## Previous Progress (2026-09-24/25)

### SDK Compatibility
- **Bug patched**: `openrouter-agent-sdk` v0.8.0 imports `OutputImage` — doesn't exist
- **Fix location**: `C:\Users\Anugraha\...\site-packages\openrouter_agent\__init__.py` (3 locations)
- **Note**: Patch lost on reinstall

### Model Selection
| Model | Basic | JSON | Array | Pipeline |
|-------|-------|------|-------|----------|
| nvidia/nemotron-3-ultra-550b:free | PASS | FAIL | PASS | - |
| poolside/laguna-s-2.1:free | FAIL | PASS | PASS | - |
| **cohere/north-mini-code:free** | **PASS** | **PASS** | **PASS** | **PASS** |
| google/gemma-4-26b-a4b-it:free | FAIL | FAIL | FAIL | - |

### OpenRouter Account
- Free tier active and working
- Current model: `cohere/north-mini-code:free`

---

## Non-Negotiable Constraints Checklist

- [x] No diagnostic language — Verifier Agent catches it
- [x] Reference ranges from lookup table, never LLM
- [x] PDF content treated as untrusted data
- [x] Exact Pydantic schemas at every agent boundary
- [x] No real patient data — all synthetic
- [x] Every explanation includes a citation (from MedlinePlus Connect, not fabricated)
- [x] Unknown LOINC cannot become normal (safety fix)
- [x] Unit mismatch handled safely
- [x] Unit abbreviations recognized as equivalent
- [x] Sex-specific ranges handled without guessing
- [ ] Two required demo moments work reliably — NOT YET TESTED
  - [ ] Injection defense demo
  - [ ] Self-correction demo

---

## P1-T4 — Failure Handling + Observability (2026-09-26, hardened)

**Status: COMPLETE**

### What was implemented

| File | Change |
|------|--------|
| `core/observability.py` | Failure taxonomy (18 types), PipelineEvent, PipelineLogger, LatencyTracker, classify_exception(), recursive privacy sanitization |
| `pipeline/orchestrator.py` | Wired PipelineLogger into real pipeline — every stage emits success/failure events with latency, retry_count, failure_type |
| `tests/test_p1_t4_failure_observability.py` | 41 deterministic tests exercising the REAL orchestrator with observability verification |

### Failure taxonomy

18 explicit failure types (15 core + 3 stage-specific):
- Core: `pdf_error`, `llm_timeout`, `llm_network_error`, `llm_auth_error`, `llm_rate_limit`, `llm_server_error`, `llm_parse_error`, `schema_validation_error`, `reference_unavailable`, `medlineplus_no_match`, `medlineplus_timeout`, `medlineplus_network_error`, `medlineplus_invalid_response`, `verification_failure`, `unknown_failure`
- Stage-specific: `extraction_failure`, `risk_classification_failure`, `explanation_failure`

### Observability (wired into real orchestrator)

- `PipelineEvent`: structured event with stage, status, failure_type, retry_count, latency_ms, reason, metadata
- `PipelineLogger`: records events, provides failure summary, stage-level logging — accepts optional parameter in `run_pipeline()`
- `LatencyTracker`: records min/max/avg latency per stage
- `classify_exception()`: honest exception-to-failure-type mapping (never guesses)

### Real pipeline evidence

- Success path: all 6 stages emit success events with latency_ms >= 0 and correct metadata
- PDF failure: only PDF stage event emitted, zero downstream LLM calls
- LLM extraction timeout: extraction stage emits llm_timeout failure, no downstream
- LLM risk failure: risk stage emits failure, no explanation
- LLM explanation failure: explanation stage emits failure
- MedlinePlus timeout/network: fallback citations, no fabrication
- Verifier rejection: verification_failure event with retry_count, bounded termination
- Upstream failure propagation: early stage failure prevents all downstream stages

### Privacy protection

- `contains_sensitive_data()`: detects SSN, phone, email, DOB, MRN patterns
- `sanitize_metadata()`: recursively sanitizes nested dicts/lists, truncates long strings
- Real pipeline events contain no raw PDF text or patient content

### Test results

```
tests/test_p1_t4_failure_observability.py: 41 passed
Full suite: 209 passed, 4 skipped (gated external tests only)
P0 regression: 87 tests pass, no regressions
```

### What was removed vs initial implementation

- Removed 13 isolated unit tests that tested observability classes in isolation
- Replaced with real pipeline integration tests that prove the orchestrator emits correct events
- Net: fewer tests but higher-quality coverage (real pipeline evidence)

### Test results

```
tests/test_p1_t4_failure_observability.py: 54 passed
Full suite: 222 passed, 4 skipped (gated external tests only)
No P0 regressions
```

### What P0 already covered (verified by P1-T4 tests)

- PDF extraction failures: corrupt, blank, image-only, truncated (P0-T3)
- Unknown LOINC → controlled unavailable (P0-T1)
- Sex-specific ranges without context → unavailable (P0-T1)
- Unit mismatch → unavailable (P0-T1)
- MedlinePlus failures: no-match, timeout, network, malformed (P0-T6)
- Untrusted URL rejection (P0-T6)
- Verifier malformed output → fail closed (P0-T4)
- Schema validation at every boundary (P0-T4)

### What P1-T4 added

- Comprehensive test coverage for all failure categories
- Observable failure propagation through pipeline stages
- Latency tracking per stage
- Privacy-safe metadata handling
- Failure matrix verification

---

## Next Steps

1. ~~Add $10 credits to OpenRouter~~ — Free tier active and working ✅
2. ~~Unit conversion fix~~ — Numerical conversion implemented ✅
3. ~~Unavailable reference propagation fix~~ — 0-0 eliminated ✅
4. Run full E2E on `normal_report.pdf`, `abnormal_report.pdf`, `injection_attack.pdf` (needs rate limit to clear or paid tier)
5. Verify injection defense works
6. Verify self-correction works
7. Test Streamlit UI
8. Run 3-5 Synthea samples for accuracy number
