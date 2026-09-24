# Lab Report Explainer Agent

Multi-agent AI pipeline that extracts lab values from PDFs, checks real reference ranges, classifies risk, generates plain-language explanations, and self-verifies for safety.

## Setup

```bash
# 1. Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API key
copy .env.example .env
# Edit .env and paste your OpenRouter API key

# 4. Run the Day 1 validation test
python tests/test_llm_client.py

# 5. Run schema + lookup tests (no API key needed)
python tests/test_schemas.py
python tests/test_reference_range.py
```

## Usage

### CLI
```bash
python -m ui.cli path/to/report.pdf
```

### Streamlit
```bash
streamlit run ui/app.py
```

## Project Structure

```
agents/          # LLM agent modules (extraction, risk, explain, verify)
core/            # Config, schemas, LLM client
data/            # Reference ranges + sample PDFs
pipeline/        # Orchestrator wiring all agents
tests/           # Validation tests
tools/           # Code-based tools (PDF extraction)
ui/              # Demo interfaces (CLI + Streamlit)
```

## Architecture

```
PDF → [pdf_extractor] → raw text
  → [extraction agent] → structured lab values
  → [reference range lookup] → range-checked values
  → [risk flagger] → classified values
  → [explainer] → plain-language explanations
  → [verifier] → approve or send back for correction
```
