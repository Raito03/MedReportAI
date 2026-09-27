"""P1-T5 — user-facing entry-point verification (deterministic, offline).

Covers the CLI demo surface end to end without an API key or network:
* usage / missing-file handling,
* the controlled "no API key configured" failure (no raw SDK traceback),
* the controlled invalid-PDF failure raised before any LLM call,
* the Streamlit entry point imports cleanly,
* core.config.llm_configured() key semantics.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.config import llm_configured

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args, api_key=""):
    """Run `python -m ui.cli ...` as a subprocess with a controlled API key."""
    env = dict(os.environ)
    env["OPENROUTER_API_KEY"] = api_key
    proc = subprocess.run(
        [sys.executable, "-m", "ui.cli", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_cli_without_args_shows_usage():
    code, out = _run_cli()
    assert code == 1
    assert "Usage: python -m ui.cli" in out


def test_cli_missing_file_reports_clearly():
    code, out = _run_cli("no_such_report.pdf")
    assert code == 1
    assert "File not found" in out
    assert "Traceback" not in out


def test_cli_without_api_key_fails_controlled():
    code, out = _run_cli("data/samples/normal_report.pdf", api_key="")
    assert code == 1
    assert "OPENROUTER_API_KEY is not configured" in out
    # The bug this guards: an empty key reached the SDK and crashed mid-
    # pipeline with an httpx LocalProtocolError traceback.
    assert "Traceback" not in out


def test_cli_placeholder_key_counts_as_unconfigured():
    code, out = _run_cli(
        "data/samples/normal_report.pdf", api_key="your-key-here"
    )
    assert code == 1
    assert "OPENROUTER_API_KEY is not configured" in out
    assert "Traceback" not in out


def test_cli_invalid_pdf_fails_controlled_before_any_llm_call(tmp_path):
    # A dummy key passes the configuration guard; the corrupt PDF must then
    # fail in stage 1 as a controlled PdfExtractionError — never a traceback.
    bad_pdf = tmp_path / "corrupt.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4\nthis is not a real PDF body\n")
    code, out = _run_cli(str(bad_pdf), api_key="sk-or-dummy-key-for-test")
    assert code == 1
    assert "PDF extraction failed" in out
    assert "Traceback" not in out


def test_streamlit_entry_point_imports_cleanly():
    import ui.app

    assert callable(ui.app.main)


@pytest.mark.parametrize(
    "key,configured",
    [
        ("", False),
        ("   ", False),
        ("your-key-here", False),
        ("sk-or-v1-a-real-looking-key", True),
    ],
)
def test_llm_configured_semantics(monkeypatch, key, configured):
    monkeypatch.setattr("core.config.OPENROUTER_API_KEY", key)
    assert llm_configured() is configured