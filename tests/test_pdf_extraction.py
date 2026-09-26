"""P0-T3 — PDF extraction robustness tests.

Deterministic: all fixture PDFs are generated programmatically at test time
(reportlab for text PDFs, a minimal hand-built PDF for the image-only case,
deterministic malformed bytes for the corrupt cases). No OpenRouter/LLM/API
access is required, and no OCR library is used or imported.

Covers:
  1. Normal text PDF            -> extraction succeeds, expected text present
  2. Multi-page PDF             -> all pages present, order preserved,
                                    blank middle page handled safely
  3. Table-heavy PDF            -> representative test names/values extracted
  4. Unusual whitespace/layout  -> values remain extractable
  5. Blank PDF                  -> controlled PdfNoTextError (reason="blank")
  6. Corrupt/malformed PDF      -> controlled PdfInvalidError, no crash,
                                    no fabricated text
  7. Image-only/scanned PDF     -> controlled PdfNoTextError
                                    (reason="image_only"), no OCR
"""

import asyncio
import os
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tools import pdf_extractor
from tools.pdf_extractor import (
    PdfExtractionError,
    PdfInvalidError,
    PdfNoTextError,
    pdf_to_text,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Deterministic malformed bytes: a PDF header with a truncated object and no
# xref/trailer — can never be parsed as a valid PDF.
CORRUPT_STATIC_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog"


# ---------------------------------------------------------------------------
# Fixture builders (programmatic — no binary fixtures committed to the repo)
# ---------------------------------------------------------------------------

def _build_normal_pdf(path: Path) -> None:
    """Single-page, plain text lab report."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, "Complete Blood Count Panel")
    c.setFont("Helvetica", 10)
    c.drawString(72, 690, "Patient: John Doe | Date: 09/26/2026")
    rows = [
        ("Glucose", "92", "mg/dL", "70-100"),
        ("Hemoglobin", "14.2", "g/dL", "12.0-17.5"),
        ("Creatinine", "0.9", "mg/dL", "0.7-1.3"),
    ]
    y = 650
    for name, val, unit, ref in rows:
        c.drawString(72, y, name)
        c.drawString(220, y, val)
        c.drawString(300, y, unit)
        c.drawString(380, y, ref)
        y -= 24
    c.showPage()
    c.save()


def _build_multipage_pdf(path: Path) -> None:
    """4 pages: text, text, blank, text — blank page must not lose other pages."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "PAGE_ONE_MARKER Alpha 111")
    c.drawString(72, 700, "Glucose 92 mg/dL")
    c.showPage()
    c.drawString(72, 720, "PAGE_TWO_MARKER Beta 222")
    c.drawString(72, 700, "Hemoglobin 14.2 g/dL")
    c.showPage()
    c.showPage()  # page 3: deliberately blank
    c.drawString(72, 720, "PAGE_THREE_MARKER Gamma 333")
    c.drawString(72, 700, "Creatinine 0.9 mg/dL")
    c.showPage()
    c.save()


def _build_table_pdf(path: Path) -> None:
    """Dense lab table with header, grid rules, and 12 result rows."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 730, "Comprehensive Metabolic Panel")
    c.setFont("Helvetica", 10)
    c.drawString(72, 712, "Patient: Jane Roe | Collected: 09/25/2026")

    cols = [72, 240, 330, 420]
    headers = ["Test Name", "Result", "Units", "Reference Range"]
    top = 690
    c.setFont("Helvetica-Bold", 10)
    for x, header in zip(cols, headers):
        c.drawString(x, top, header)
    c.line(72, top - 6, 540, top - 6)

    rows = [
        ("Glucose", "145", "mg/dL", "70-100"),
        ("Hemoglobin", "10.1", "g/dL", "12.0-17.5"),
        ("Hemoglobin A1c", "7.2", "%", "4.0-5.6"),
        ("Total Cholesterol", "245", "mg/dL", "<200"),
        ("Triglycerides", "195", "mg/dL", "<150"),
        ("HDL Cholesterol", "35", "mg/dL", "40-60"),
        ("LDL Cholesterol", "155", "mg/dL", "<100"),
        ("ALT", "62", "U/L", "7-56"),
        ("AST", "38", "U/L", "10-40"),
        ("Creatinine", "1.8", "mg/dL", "0.7-1.3"),
        ("BUN", "28", "mg/dL", "7-20"),
        ("WBC", "12.5", "K/uL", "4.5-11.0"),
    ]
    c.setFont("Helvetica", 10)
    y = top - 24
    for name, val, unit, ref in rows:
        c.drawString(cols[0], y, name)
        c.drawString(cols[1], y, val)
        c.drawString(cols[2], y, unit)
        c.drawString(cols[3], y, ref)
        c.line(72, y - 6, 540, y - 6)
        y -= 20
    c.showPage()
    c.save()


def _build_whitespace_pdf(path: Path) -> None:
    """Multiple spaces, tab characters, irregular line breaks, columns."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 11)
    # multiple spaces between label and value
    c.drawString(72, 720, "Glucose:      92      mg/dL")
    # literal tab characters inside the text string (PDF has no tab
    # semantics — pdfminer maps the raw byte through the font encoding, so
    # only fragment presence is guaranteed for this line)
    c.drawString(72, 700, "Hemoglobin\t14.2\tg/dL")
    # tab-stop style layout jump (how tabs are really rendered in PDFs)
    c.drawString(72, 680, "Potassium")
    c.drawString(220, 680, "4.1")
    c.drawString(330, 680, "mmol/L")
    # irregular line breaks (wrapped label/value on separate lines)
    c.drawString(72, 664, "Total")
    c.drawString(72, 648, "Cholesterol")
    c.drawString(72, 632, "185")
    # column alignment: second column on the same line
    c.drawString(300, 720, "TSH")
    c.drawString(430, 720, "4.5")
    c.drawString(300, 700, "mIU/L")
    c.showPage()
    c.save()


def _build_blank_pdf(path: Path) -> None:
    """A valid PDF with exactly one empty page."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.showPage()
    c.save()


def _build_image_only_pdf(path: Path) -> None:
    """Minimal valid PDF: one page with an embedded image and NO text.

    Built by hand (stdlib zlib only) so no OCR/PIL dependency is needed and
    the bytes are fully deterministic.
    """
    raw = bytes((x * 17 + y * 31) % 256 for y in range(8) for x in range(8))
    compressed = zlib.compress(raw)

    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"),
        4: (b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            b"/Length " + str(len(compressed)).encode() + b" >>"
            b"\nstream\n" + compressed + b"\nendstream"),
        5: (b"<< /Length 28 >>\nstream\n"
            b"q 200 0 0 200 0 0 cm /Im0 Do Q\nendstream"),
    }

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in sorted(objects):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


@pytest.fixture(scope="session")
def pdf_fixtures(tmp_path_factory) -> dict:
    """Generate all P0-T3 fixture PDFs once per test session."""
    root = tmp_path_factory.mktemp("p0t3_pdfs")
    paths = {
        "normal": root / "normal.pdf",
        "multipage": root / "multipage.pdf",
        "table": root / "table.pdf",
        "whitespace": root / "whitespace.pdf",
        "blank": root / "blank.pdf",
        "image_only": root / "image_only.pdf",
        "corrupt_static": root / "corrupt_static.pdf",
        "corrupt_truncated": root / "corrupt_truncated.pdf",
        "corrupt_empty": root / "corrupt_empty.pdf",
    }

    _build_normal_pdf(paths["normal"])
    _build_multipage_pdf(paths["multipage"])
    _build_table_pdf(paths["table"])
    _build_whitespace_pdf(paths["whitespace"])
    _build_blank_pdf(paths["blank"])
    _build_image_only_pdf(paths["image_only"])
    paths["corrupt_static"].write_bytes(CORRUPT_STATIC_BYTES)
    # Deterministic truncation of a real reportlab-produced PDF.
    valid = paths["normal"].read_bytes()
    paths["corrupt_truncated"].write_bytes(valid[: max(64, len(valid) // 3)])
    paths["corrupt_empty"].write_bytes(b"")
    return paths


# ---------------------------------------------------------------------------
# 1. Normal text PDF
# ---------------------------------------------------------------------------

def test_normal_pdf_extracts_expected_text(pdf_fixtures):
    text = pdf_to_text(str(pdf_fixtures["normal"]))
    assert isinstance(text, str)
    assert "Complete Blood Count Panel" in text
    assert "Patient: John Doe" in text
    # name + value + unit remain adjacent enough for downstream extraction
    assert re.search(r"Glucose\s+92\s+mg/dL", text)
    assert re.search(r"Hemoglobin\s+14\.2\s+g/dL", text)
    assert re.search(r"Creatinine\s+0\.9\s+mg/dL", text)
    # newlines preserved (needed by the LLM extraction stage)
    assert "\n" in text
    # extraction is deterministic
    assert pdf_to_text(str(pdf_fixtures["normal"])) == text


# ---------------------------------------------------------------------------
# 2. Multi-page PDF
# ---------------------------------------------------------------------------

def test_multipage_pdf_all_pages_present_in_order(pdf_fixtures):
    text = pdf_to_text(str(pdf_fixtures["multipage"]))
    for marker in ("PAGE_ONE_MARKER", "PAGE_TWO_MARKER", "PAGE_THREE_MARKER"):
        assert marker in text, f"missing text from page: {marker}"
    # page ordering preserved
    assert text.index("PAGE_ONE_MARKER") < text.index("PAGE_TWO_MARKER")
    assert text.index("PAGE_TWO_MARKER") < text.index("PAGE_THREE_MARKER")
    # blank page 3 must not swallow text from pages 2 and 4
    assert "Glucose 92" in text
    assert "Creatinine 0.9" in text
    # deterministic combining of pages
    assert pdf_to_text(str(pdf_fixtures["multipage"])) == text


# ---------------------------------------------------------------------------
# 3. Table-heavy PDF
# ---------------------------------------------------------------------------

def test_table_heavy_pdf_keeps_names_and_values(pdf_fixtures):
    text = pdf_to_text(str(pdf_fixtures["table"]))
    assert re.search(r"Test\s+Name\s+Result\s+Units\s+Reference\s+Range", text)
    assert re.search(r"Glucose\s+145\s+mg/dL", text)
    assert re.search(r"Hemoglobin\s+10\.1\s+g/dL", text)
    assert re.search(r"Hemoglobin\s+A1c\s+7\.2\s+%", text)
    assert re.search(r"Total\s+Cholesterol\s+245\s+mg/dL", text)
    assert re.search(r"Creatinine\s+1\.8\s+mg/dL", text)
    assert re.search(r"WBC\s+12\.5\s+K/uL", text)
    # reference ranges survive too (not silently discarded)
    assert "70-100" in text and "<200" in text


# ---------------------------------------------------------------------------
# 4. Unusual whitespace / alignment
# ---------------------------------------------------------------------------

def test_unusual_whitespace_and_alignment_still_extractable(pdf_fixtures):
    text = pdf_to_text(str(pdf_fixtures["whitespace"]))
    # multiple spaces between label and value: usable, value stays paired
    assert re.search(r"Glucose:\s+92\s+mg/dL", text)
    # tab-stop style column jump: value and unit stay paired
    assert re.search(r"Potassium\s+4\.1\s+mmol/L", text)
    # literal tab characters: extraction must not crash and every fragment
    # must remain present (PDF has no tab semantics, so only presence of the
    # fragments is guaranteed — no fabricated/lost content)
    assert "Hemoglobin" in text
    assert "14.2" in text
    assert "g/dL" in text
    # irregular line breaks keep every fragment
    assert "Total" in text and "Cholesterol" in text and "185" in text
    # column alignment on the same line
    assert re.search(r"TSH\s+4\.5", text)
    assert "mIU/L" in text


# ---------------------------------------------------------------------------
# 5. Blank PDF
# ---------------------------------------------------------------------------

def test_blank_pdf_raises_controlled_no_text_failure(pdf_fixtures):
    with pytest.raises(PdfNoTextError) as excinfo:
        pdf_to_text(str(pdf_fixtures["blank"]))
    error = excinfo.value
    assert error.reason == "blank"
    assert isinstance(error, PdfExtractionError)
    assert isinstance(error, ValueError)  # project error convention
    assert not isinstance(error, PdfInvalidError)  # file itself is valid
    assert "blank" in str(error).lower()


# ---------------------------------------------------------------------------
# 6. Corrupt / malformed PDF
# ---------------------------------------------------------------------------

def test_corrupt_pdfs_raise_controlled_invalid_failure(pdf_fixtures):
    for key in ("corrupt_static", "corrupt_truncated", "corrupt_empty"):
        with pytest.raises(PdfInvalidError) as excinfo:
            pdf_to_text(str(pdf_fixtures[key]))
        error = excinfo.value
        # controlled, meaningful failure — not a bare crash, and crucially
        # not misclassified as "successfully extracted no text"
        assert isinstance(error, PdfExtractionError)
        assert not isinstance(error, PdfNoTextError)
        assert len(str(error)) > 0


def test_missing_file_raises_file_not_found(tmp_path):
    """Missing path keeps its pre-existing, already-controlled failure."""
    with pytest.raises(FileNotFoundError):
        pdf_to_text(str(tmp_path / "does_not_exist.pdf"))


# ---------------------------------------------------------------------------
# 7. Image-only / scanned PDF (OCR out of scope)
# ---------------------------------------------------------------------------

def test_image_only_pdf_raises_unsupported_no_text_failure(pdf_fixtures):
    path = str(pdf_fixtures["image_only"])
    # Sanity-check the fixture really is image-only: an image and no text.
    with pdfplumber.open(path) as pdf:
        assert len(pdf.pages) == 1
        assert pdf.pages[0].extract_text() in (None, "")
        assert len(pdf.pages[0].images) > 0

    with pytest.raises(PdfNoTextError) as excinfo:
        pdf_to_text(path)
    error = excinfo.value
    assert error.reason == "image_only"
    message = str(error)
    assert "OCR" in message
    assert "unsupported" in message.lower()


def test_extractor_contains_no_ocr_dependencies():
    """OCR must not be introduced — guard against future regressions."""
    source = Path(pdf_extractor.__file__).read_text(encoding="utf-8").lower()
    for token in ("tesseract", "pytesseract", "easyocr", "paddleocr",
                  "keras_ocr", "ocrmypdf"):
        assert token not in source, f"OCR dependency introduced: {token}"


# ---------------------------------------------------------------------------
# Regression: existing committed sample PDFs still extract
# ---------------------------------------------------------------------------

def test_existing_normal_sample_still_extracts():
    sample = REPO_ROOT / "data" / "samples" / "normal_report.pdf"
    assert sample.exists(), "committed sample fixture missing"
    text = pdf_to_text(str(sample))
    assert "Glucose" in text and "92" in text
    assert "Platelet Count" in text


def test_existing_abnormal_sample_still_extracts():
    sample = REPO_ROOT / "data" / "samples" / "abnormal_report.pdf"
    assert sample.exists(), "committed sample fixture missing"
    text = pdf_to_text(str(sample))
    assert "Glucose" in text and "145" in text


# ---------------------------------------------------------------------------
# Integration: orchestrator fails before any LLM call (no OpenRouter needed)
# ---------------------------------------------------------------------------

def test_orchestrator_fails_controlled_before_any_llm_call(pdf_fixtures):
    from pipeline.orchestrator import run_pipeline

    # Extraction failure happens at step 1/5, before any network/LLM call.
    with pytest.raises(PdfNoTextError):
        asyncio.run(run_pipeline(str(pdf_fixtures["blank"])))


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

