# Lab Report Explainer Agent

A multi-agent AI pipeline that takes a lab report PDF, extracts values via code-based extraction, checks them against *real* reference ranges (never LLM-guessed), flags risk level, generates plain-language explanations with citations, and **verifies its own output for safety before showing it to the user.**

Built as an internal TCS demo/evaluation project — 8-person team, presented to reviewers.

---

## Why This Is an Agentic System, Not "Just a Good Prompt"

| If it were just a prompt | What we actually built |
|---|---|
| LLM guesses reference ranges from memory | A **tool call** retrieves real ranges from a local LOINC/NHANES lookup table — never guessed |
| One shot, no self-check | A dedicated **Verifier Agent** checks output and can trigger a redo |
| No defense against malicious input | PDF content is treated as **untrusted data** — demonstrated against prompt-injection |
| Unstructured text output | Every agent boundary uses strict, validated **Pydantic JSON schemas** |

---

## Architecture

```
PDF → [pdf_extractor] → raw text                    (code — pdfplumber)
  → [extraction agent] → structured lab values      (LLM call #1)
  → [reference range lookup] → range-checked values  (tool call — never LLM)
  → [risk flagger] → classified values              (LLM call #2 — grounded)
  → [explainer] → plain-language explanations       (LLM call #3)
  → [verifier] → approve or send back               (code regex + LLM call #4)
```

Each agent has:
- A Pydantic `input_schema` and `output_schema` (Section 5 of spec)
- An SDK `tool()` definition with both schemas attached
- Async execution via `openrouter-agent-sdk`'s `call_model()` loop

---

## Tech Stack

| Component | Choice |
|---|---|
| LLM API | [OpenRouter](https://openrouter.ai) via `openrouter-agent-sdk` |
| Model | `cohere/north-mini-code:free` (tested against 4 free models) |
| PDF extraction | `pdfplumber` (code-based, not LLM) |
| Reference ranges | 26 LOINC codes + NHANES ranges (local JSON lookup) |
| Orchestration | Sequential Python orchestrator with retry loop |
| Demo interface | CLI + Streamlit |

---

## Project Structure

```
├── agents/                  # AI agent modules (each with SDK tool + schemas)
│   ├── extraction.py        #   LLM call #1: raw text → ExtractedLabValue[]
│   ├── reference_range.py   #   Tool call (NO LLM): lookup against JSON table
│   ├── risk_flagger.py      #   LLM call #2: → RiskFlaggedValue[]
│   ├── explainer.py         #   LLM call #3: → FinalExplanation[]
│   └── verifier.py          #   Code regex + LLM: → VerifierResult
│
├── core/                    # Shared infrastructure
│   ├── config.py            #   Settings loader (.env)
│   ├── llm_client.py        #   OpenRouter client singleton
│   └── schemas.py           #   Pydantic models for all agent I/O
│
├── pipeline/
│   └── orchestrator.py      # Wires all agents + self-correction retry loop
│
├── tools/
│   ├── pdf_extractor.py     # pdf_to_text(file_path) → str
│   └── generate_samples.py  # Creates test PDFs
│
├── data/
│   ├── reference_ranges.json # 26 LOINC codes + NHANES ranges
│   └── samples/              # Synthetic test PDFs
│       ├── normal_report.pdf
│       ├── abnormal_report.pdf
│       └── injection_attack.pdf
│
├── tests/
│   ├── test_schemas.py       # Pydantic model validation
│   ├── test_reference_range.py # Lookup correctness
│   ├── test_pdf_extraction.py # PDF extraction robustness (deterministic, no API key)
│   └── test_llm_client.py    # SDK connectivity + tool loop
│
├── ui/
│   ├── cli.py               # python -m ui.cli report.pdf
│   └── app.py               # streamlit run ui/app.py
│
├── PROJECT.md               # Full spec document (source of truth)
├── PROGRESS.md              # Session state tracker
└── requirements.txt
```

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/Raito03/MedReportAI.git
cd MedReportAI

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API key
copy .env.example .env       # Windows
# cp .env.example .env       # Mac/Linux
# Edit .env and paste your OpenRouter API key

# 5. Run tests
PYTHONPATH=. python tests/test_schemas.py
PYTHONPATH=. python tests/test_reference_range.py
PYTHONPATH=. python tests/test_llm_client.py       # requires API key
```

> **Note:** On Windows, set `PYTHONPATH=G:\MedReportAI` or use `set PYTHONPATH=...` before running tests.

---

## Usage

### CLI
```bash
python -m ui.cli data/samples/normal_report.pdf
```

### Streamlit
```bash
streamlit run ui/app.py
```

---

## Sample Outputs

Running on a normal blood panel (14 values):

```
[1/5] Extracting text from PDF...         552 characters
[2/5] Extracting lab values via LLM...    14 values found
[3/5] Looking up reference ranges...      14 range-checked
[4/5] Classifying risk levels...          14 classified
[5/5] Generating explanations...
      Verifier PASSED
```

Each value produces:
- Plain-language explanation (no diagnostic claims)
- 3 suggested questions for the doctor
- MedlinePlus citation

---

## Data Schemas

Every agent boundary uses strict Pydantic models (matching PROJECT.md Section 5):

<details>
<summary>ExtractedLabValue</summary>

```json
{
  "test_name": "Glucose",
  "loinc_code": "2345-7",
  "value": 92.0,
  "unit": "mg/dL"
}
```
</details>

<details>
<summary>RangeCheckedValue</summary>

```json
{
  "test_name": "Glucose",
  "loinc_code": "2345-7",
  "value": 92.0,
  "unit": "mg/dL",
  "reference_low": 70.0,
  "reference_high": 100.0,
  "in_range": true
}
```
</details>

<details>
<summary>RiskFlaggedValue</summary>

```json
{
  "test_name": "Glucose",
  "value": 92.0,
  "unit": "mg/dL",
  "status": "normal",
  "reasoning": "Within reference range"
}
```
</details>

<details>
<summary>FinalExplanation</summary>

```json
{
  "test_name": "Glucose",
  "explanation": "Glucose measures blood sugar levels; results in this range are generally considered normal.",
  "doctor_questions": ["How often should glucose be checked?", "Are there dietary factors that affect readings?"],
  "citation": "MedlinePlus: Blood Glucose Test"
}
```
</details>

<details>
<summary>VerifierResult</summary>

```json
{
  "passed": true,
  "issues_found": [],
  "action": "approve"
}
```
</details>

---

## Required Demo Moments

1. **Prompt-injection defense** — `data/samples/injection_attack.pdf` contains hidden text instructing the system to mark everything as normal. The pipeline ignores it because extracted content is treated as data, not instructions.

2. **Self-correction** — The Verifier Agent catches diagnostic language (e.g., "Your glucose level...") and triggers a retry with corrected output.

---

## Model Selection

Tested 4 free OpenRouter models against SDK compatibility:

| Model | Basic Chat | JSON Output | JSON Array | Pipeline |
|-------|-----------|-------------|------------|----------|
| nvidia/nemotron-3-ultra-550b-a55b:free | PASS | FAIL | PASS | - |
| poolside/laguna-s-2.1:free | FAIL | PASS | PASS | - |
| **cohere/north-mini-code:free** | **PASS** | **PASS** | **PASS** | **PASS** |
| google/gemma-4-26b-a4b-it:free | FAIL | FAIL | FAIL | - |

---

## Non-Negotiable Constraints

- [x] No diagnostic language — verified by Verifier Agent, not just prompted
- [x] Reference ranges from lookup table, never LLM memory
- [x] PDF content treated as untrusted data
- [x] Exact Pydantic schemas at every agent boundary
- [x] No real patient data — all synthetic
- [x] Every explanation includes a citation
- [ ] Both demo moments work reliably (in progress)

---

## Roadmap (Out of Scope for Demo)

- Trend Agent / multi-visit patient history
- Full eval harness / large test suite
- UI polish beyond functional
- Multi-language support
- Real user data storage

---

## License

Internal project — not for public distribution.
