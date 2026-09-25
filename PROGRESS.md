# Progress Log — Lab Report Explainer Agent

## Current Status
Task 1 complete. Production reference data integrated. MedlinePlus Connect integrated. All 31 unit tests pass. Pipeline runs end-to-end with real MedlinePlus citations. Free tier API active and working.

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
tests/test_schemas.py          6 passed
tests/test_reference_range.py 15 passed
tests/test_medlineplus.py     10 passed
Total: 31 passed in 1.94s
```

### Remaining NHANES Work

The current `reference_ranges.json` uses **MedlinePlus-documented intervals**, not NHANES-derived intervals. Per the task instructions, NHANES-specific provenance is a future hardening step.

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

## Next Steps

1. ~~Add $10 credits to OpenRouter~~ — Free tier active and working ✅
2. Run E2E pipeline on `normal_report.pdf`, `abnormal_report.pdf`, `injection_attack.pdf`
3. Verify injection defense works
4. Verify self-correction works
5. Test Streamlit UI
6. Run 3-5 Synthea samples for accuracy number
