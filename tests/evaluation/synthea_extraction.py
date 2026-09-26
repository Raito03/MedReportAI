"""Deterministic, offline stand-in for the extraction model (P1-T3).

The evaluation must exercise the REAL extraction path — PDF bytes -> pdfplumber
text -> ``agents.extraction.extract_lab_values()`` -> ``ExtractedLabValue`` —
while staying deterministic and offline. Only the LLM call itself is replaced
(the project's injection point ``call_model(client, params)``), exactly like the
P0-T5 test infrastructure does.

The stand-in reads the same prompt the model would receive (SYSTEM_PROMPT +
``USER INPUT:`` + the text pdfplumber produced) and returns the JSON array the
prompt asks for, parsed from that text with deterministic rules:

* a result row is a line containing a token the project already knows as a unit
  (canonical units of ``data/supported_labs.json`` plus the units of the
  project's own conversion/alias tables — no new unit knowledge is invented);
* the token directly in front of that unit is the numeric result, everything
  before it is the test name (an embedded LOINC token is separated out);
* a LOINC code is only reported when the report text carries one; otherwise
  ``"unknown"`` is returned and the project's own post-processing
  (``resolve_loinc_from_test_name``) resolves the code.

Consequence, stated explicitly in PROGRESS.md/ROADMAP.md: the measured accuracy
describes the DETERMINISTIC part of the extraction path (PDF -> text -> schema
-> LOINC resolution), not live model quality.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import patch

from agents.extraction import SYSTEM_PROMPT, extract_lab_values
from agents.reference_range import _UNIT_ALIASES, _UNIT_CONVERSIONS, SUPPORTED_LABS_PATH
from core.schemas import ExtractedLabValue
from tests.fake_llm import FakeLLM
from tools.pdf_extractor import pdf_to_text

PROMPT_MARKER = "USER INPUT:"
NUMERIC_TOKEN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
LOINC_TOKEN = re.compile(r"^(\d{1,5}-\d)$")
MAX_UNIT_TOKENS = 2


def _load_canonical_units() -> List[str]:
    with open(SUPPORTED_LABS_PATH, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return [test["canonical_unit"] for test in raw.get("tests", [])]


def unit_vocabulary() -> List[str]:
    """Units the deterministic stand-in recognises (project knowledge only)."""
    units = set(_load_canonical_units())
    units.update(_UNIT_ALIASES.keys())
    for source, target in _UNIT_CONVERSIONS:
        units.add(source)
        units.add(target)
    units.discard("")
    # Longest first so multi-token units ("million cells/mcL") win over any
    # shorter unit that starts the same way.
    return sorted(units, key=lambda unit: (-len(unit.split()), -len(unit)))


_UNIT_VOCABULARY = unit_vocabulary()
_UNIT_KEYS = {(" ".join(unit.split())).lower(): unit for unit in _UNIT_VOCABULARY}


def _normalize_token(token: str) -> str:
    return " ".join(token.split()).lower()


def _strip_token_punctuation(token: str) -> str:
    return token.strip(" \t,;:").strip()


def _match_unit(tokens: Sequence[str], start: int) -> Optional[str]:
    """Return the unit text starting at ``tokens[start]`` (longest match wins)."""
    for length in range(MAX_UNIT_TOKENS, 0, -1):
        if start + length > len(tokens):
            continue
        candidate = " ".join(tokens[start:start + length])
        matched = _UNIT_KEYS.get(_normalize_token(candidate))
        if matched is not None:
            return matched
    return None


def _clean_test_name(name_tokens: Sequence[str]) -> str:
    return _strip_token_punctuation(" ".join(name_tokens))


def parse_report_text(raw_text: str) -> List[Dict[str, Any]]:
    """Deterministic stand-in for the model's answer on one report's text."""
    observations: List[Dict[str, Any]] = []
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        tokens = line.split()
        for index, _ in enumerate(tokens):
            if index == 0:
                continue
            unit = _match_unit(tokens, index)
            if unit is None:
                continue
            value_token = tokens[index - 1]
            if not NUMERIC_TOKEN.match(value_token):
                continue
            name_tokens = list(tokens[:index - 1])
            loinc_code = "unknown"
            remaining: List[str] = []
            for token in name_tokens:
                candidate = _strip_token_punctuation(token)
                match = LOINC_TOKEN.match(candidate)
                if match is not None:
                    loinc_code = candidate
                elif candidate.lower() not in ("loinc", ""):
                    remaining.append(token)
            test_name = _clean_test_name(remaining)
            if not test_name:
                continue
            observations.append({
                "test_name": test_name,
                "loinc_code": loinc_code,
                "value": float(value_token),
                "unit": unit,
            })
            break
    return observations


def prompt_input_text(prompt: str) -> str:
    """Return the raw report text the prompt carried (after the USER INPUT marker)."""
    marker_index = prompt.find(PROMPT_MARKER)
    if marker_index == -1:
        raise ValueError("Prompt does not contain the USER INPUT marker")
    return prompt[marker_index + len(PROMPT_MARKER):].lstrip("\n")


class OfflineClient:
    """Client stub for the patched injection point.

    The LLM call is faked, so this object must never be used; any attribute
    access fails loudly instead of reaching the network.
    """

    def __getattr__(self, item):  # pragma: no cover - defensive
        raise AssertionError(
            "Offline evaluation must never use a real LLM client "
            f"(attempted attribute: {item})")


@dataclass
class ExtractionRun:
    """Everything one report's extraction produced, plus its provenance."""
    report_id: str
    pdf_path: Path
    raw_text: str
    prompt: str
    planned_response: List[Dict[str, Any]]
    observations: List[ExtractedLabValue]
    llm_calls: int

    @property
    def prompt_carried_pdf_text(self) -> bool:
        """Proof that the real PDF extraction stage fed the extraction step."""
        return self.raw_text in (self.prompt or "")


def extract_report_observations(pdf_path: Path, report_id: str = "") -> ExtractionRun:
    """Run the real extraction path on one PDF with a deterministic model stand-in."""
    pdf_path = Path(pdf_path)
    raw_text = pdf_to_text(str(pdf_path))  # real PDF -> text extraction
    planned_response = parse_report_text(raw_text)
    fake = FakeLLM(responses=[planned_response])
    with patch("agents.extraction.call_model", fake), \
            patch("agents.extraction.get_client", lambda *args, **kwargs: OfflineClient()):
        observations = asyncio.run(extract_lab_values(raw_text))
    return ExtractionRun(
        report_id=report_id or pdf_path.stem,
        pdf_path=pdf_path,
        raw_text=raw_text,
        prompt=fake.last_prompt or "",
        planned_response=planned_response,
        observations=list(observations),
        llm_calls=fake.call_count,
    )


def extract_dataset(ground_truth: Dict[str, Any], reports_dir: Path
                    ) -> Tuple[Dict[str, List[ExtractedLabValue]],
                               Dict[str, ExtractionRun]]:
    """Run the extraction path over every report of the ground truth."""
    reports_dir = Path(reports_dir)
    by_report: Dict[str, List[ExtractedLabValue]] = {}
    runs: Dict[str, ExtractionRun] = {}
    for report in ground_truth["reports"]:
        report_id = report["report_id"]
        pdf_path = reports_dir / f"{report_id}.pdf"
        run = extract_report_observations(pdf_path, report_id=report_id)
        runs[report_id] = run
        by_report[report_id] = run.observations
    return by_report, runs

