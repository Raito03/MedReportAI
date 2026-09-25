# Progress Log — Lab Report Explainer Agent

## Current Status
Task 1 complete + hardened. Production reference data integrated. MedlinePlus Connect integrated. 40 unit tests pass (24 reference range, 6 schema, 10 MedlinePlus). Unit conversion and unavailable-range propagation fixed. E2E pipeline steps 1-4 verified on normal_report.pdf (explanation step rate-limited).

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
2. ~~Unit conversion fix~~ — Numerical conversion implemented ✅
3. ~~Unavailable reference propagation fix~~ — 0-0 eliminated ✅
4. Run full E2E on `normal_report.pdf`, `abnormal_report.pdf`, `injection_attack.pdf` (needs rate limit to clear or paid tier)
5. Verify injection defense works
6. Verify self-correction works
7. Test Streamlit UI
8. Run 3-5 Synthea samples for accuracy number
