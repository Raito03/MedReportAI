"""Deterministic Synthea-style report PDF generation for P1-T3.

The report PDFs in ``data/synthea_reports/`` are RENDERED FROM the ground truth
(``data/synthea_ground_truth.json``) by this module, never the other way round.
Determinism comes from reportlab's ``invariant`` mode: identical input produces
byte-identical PDF files (checked by the ``--check`` mode of
``generate_synthea_reports.py`` and by the test suite). No date/time, random
value or environment value ever enters the output.

Layouts are deliberately varied (aligned columns with and without a LOINC
column, wide column spacing, a ruled table, ``Test: value unit`` labelled
lines, a two-page report, and a repeat-draw report containing the same test
name twice) because real Synthea-style/laboratory PDFs vary in exactly these
ways.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as pdf_canvas

PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN_X = 54.0
TOP_Y = PAGE_HEIGHT - 54.0
LINE_HEIGHT = 18.0
BODY_FONT = "Helvetica"
BODY_BOLD = "Helvetica-Bold"
BODY_SIZE = 10.0

TABLE_LAYOUTS = (
    "columns_no_loinc",
    "columns_with_loinc",
    "wide_spacing",
    "ruled_table",
    "duplicate_rows",
    "multipage",
    "multipage_loinc",
    "labelled_lines",
)

LOINC_COLUMN_LAYOUTS = ("columns_with_loinc", "ruled_table", "duplicate_rows")
ROWS_PER_PAGE = 3


def printed_value_text(observation: Dict[str, Any]) -> str:
    """How the numeric result is rendered in the PDF.

    ``printed_value_text`` documents formatting variations (``250`` for the
    numeric expectation ``250.0``, ``0.90`` for ``0.9``); the value compared
    against the ground truth is always the numeric ``value`` field.
    """
    if observation.get("printed_value_text") is not None:
        return str(observation["printed_value_text"])
    value = observation.get("value")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _columns_for(layout: str) -> List[Tuple[str, float]]:
    if layout in LOINC_COLUMN_LAYOUTS:
        columns = [("Test Name", 175.0), ("LOINC", 65.0), ("Result", 60.0),
                   ("Units", 80.0), ("Reference Range", 110.0)]
    elif layout == "multipage_loinc":
        columns = [("Test Name", 175.0), ("LOINC", 95.0), ("Result", 60.0),
                   ("Units", 80.0), ("Reference Range", 110.0)]
    elif layout == "wide_spacing":
        columns = [("Test Name", 235.0), ("Result", 105.0), ("Units", 115.0),
                   ("Reference Range", 125.0)]
    else:
        columns = [("Test Name", 210.0), ("Result", 70.0), ("Units", 85.0),
                   ("Reference Range", 120.0)]
    if layout == "duplicate_rows":
        columns = columns + [("Collection", 90.0)]
    return columns


def _cell_text(observation: Dict[str, Any], header: str, layout: str) -> str:
    if header == "Test Name":
        return str(observation.get("test_name", ""))
    if header == "LOINC":
        code = str(observation.get("loinc_code", ""))
        return f"LOINC: {code}" if layout == "multipage_loinc" else code
    if header == "Result":
        return printed_value_text(observation)
    if header == "Units":
        return str(observation.get("unit", ""))
    if header == "Reference Range":
        return str(observation.get("reference_range_text", ""))
    if header == "Collection":
        return str(observation.get("pdf_row_label", ""))
    raise KeyError(f"Unknown column: {header}")


def _draw_text_line(pdf: pdf_canvas.Canvas, text: str, y: float,
                    font: str = BODY_FONT, size: float = BODY_SIZE) -> float:
    pdf.setFont(font, size)
    pdf.drawString(MARGIN_X, y, text)
    return y - LINE_HEIGHT


def _draw_report_header(pdf: pdf_canvas.Canvas, report: Dict[str, Any],
                        page_number: int, page_count: int) -> float:
    y = TOP_Y
    y = _draw_text_line(
        pdf, "MedReportAI SYNTHETIC Laboratory Report (Synthea-style)",
        y, font=BODY_BOLD, size=13.0)
    y = _draw_text_line(pdf, f"Panel: {report.get('panel', '')}", y)
    y = _draw_text_line(
        pdf,
        f"Patient: {report.get('synthetic_patient', '')}    "
        f"Collected: {report.get('collection_date', '')}    "
        f"Specimen: {report.get('specimen', '')}",
        y)
    y = _draw_text_line(pdf, "SYNTHETIC DATA - NOT A REAL PATIENT RECORD", y)
    y = _draw_text_line(pdf, f"Page {page_number} of {page_count}", y)
    return y - LINE_HEIGHT / 2


def _draw_table_header(pdf: pdf_canvas.Canvas, columns: List[Tuple[str, float]],
                       y: float, layout: str) -> float:
    x = MARGIN_X
    pdf.setFont(BODY_BOLD, BODY_SIZE)
    for header, width in columns:
        pdf.drawString(x, y, header)
        x += width
    if layout == "ruled_table":
        total_width = sum(width for _, width in columns)
        pdf.setLineWidth(0.8)
        pdf.line(MARGIN_X, y + 3.0, MARGIN_X + total_width, y + 3.0)
        pdf.line(MARGIN_X, y - LINE_HEIGHT + 4.0, MARGIN_X + total_width,
                 y - LINE_HEIGHT + 4.0)
    return y - LINE_HEIGHT


def _draw_row(pdf: pdf_canvas.Canvas, columns: List[Tuple[str, float]],
              values: List[str], y: float) -> float:
    x = MARGIN_X
    pdf.setFont(BODY_FONT, BODY_SIZE)
    for (_, width), value in zip(columns, values):
        pdf.drawString(x, y, value)
        x += width
    return y - LINE_HEIGHT


def _render_table(pdf: pdf_canvas.Canvas, report: Dict[str, Any]) -> None:
    layout = report.get("layout", "columns_no_loinc")
    columns = _columns_for(layout)
    observations = report["expected_observations"]
    paginated = layout in ("multipage", "multipage_loinc")
    pages = ([observations[i:i + ROWS_PER_PAGE]
              for i in range(0, len(observations), ROWS_PER_PAGE)]
             if paginated else [observations])
    page_count = len(pages)
    total_width = sum(width for _, width in columns)

    for page_index, page_rows in enumerate(pages, start=1):
        y = _draw_report_header(pdf, report, page_index, page_count)
        if layout == "ruled_table":
            pdf.setLineWidth(0.5)
            pdf.rect(
                MARGIN_X - 4.0,
                TOP_Y - 70.0 - LINE_HEIGHT * (len(page_rows) + 1),
                total_width + 8.0,
                LINE_HEIGHT * (len(page_rows) + 2) + 6.0)
        y = _draw_table_header(pdf, columns, y, layout)
        for observation in page_rows:
            values = [_cell_text(observation, header, layout) for header, _ in columns]
            y = _draw_row(pdf, columns, values, y)
            if layout == "ruled_table":
                pdf.setLineWidth(0.5)
                pdf.line(MARGIN_X - 4.0, y + LINE_HEIGHT - 4.0,
                         MARGIN_X + total_width + 4.0, y + LINE_HEIGHT - 4.0)
        if layout == "duplicate_rows":
            _draw_text_line(
                pdf,
                "Repeat draw performed at the clinician's request; both results are "
                "reported and must be kept separately.",
                y - LINE_HEIGHT / 2)
        if page_index < page_count:
            pdf.showPage()
    pdf.showPage()


def _render_labelled_lines(pdf: pdf_canvas.Canvas, report: Dict[str, Any]) -> None:
    y = _draw_report_header(pdf, report, 1, 1)
    y = _draw_text_line(pdf, "Result summary", y, font=BODY_BOLD)
    for observation in report["expected_observations"]:
        line = (f"{observation.get('test_name', '')}: "
                f"{printed_value_text(observation)} {observation.get('unit', '')}   "
                f"(reference range: {observation.get('reference_range_text', '')})")
        y = _draw_text_line(pdf, line, y)
    _draw_text_line(pdf, "End of synthetic report", y - LINE_HEIGHT / 2)
    pdf.showPage()


def render_report_pdf(report: Dict[str, Any], path: Path) -> Path:
    """Render one ground-truth report to a deterministic PDF file."""
    layout = report.get("layout", "columns_no_loinc")
    if layout not in TABLE_LAYOUTS:
        raise ValueError(
            f"Unknown layout {layout!r} for report {report.get('report_id')!r}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pdf_canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    pdf.setTitle(f"Synthetic laboratory report {report.get('report_id', '')}")
    pdf.setAuthor("MedReportAI synthetic fixture generator")
    pdf.setSubject("Synthea-style synthetic laboratory report")
    if layout == "labelled_lines":
        _render_labelled_lines(pdf, report)
    else:
        _render_table(pdf, report)
    pdf.save()
    return path


def generate_all(ground_truth: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    """Render every report of the ground truth into ``out_dir``."""
    generated: Dict[str, Path] = {}
    for report in ground_truth["reports"]:
        generated[report["report_id"]] = render_report_pdf(
            report, Path(out_dir) / f"{report['report_id']}.pdf")
    return generated


def sha256_of(path: Path) -> str:
    """SHA-256 of a generated fixture (determinism proof)."""
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for(ground_truth: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """Manifest describing the deterministic report fixtures."""
    entries = []
    for report in ground_truth["reports"]:
        pdf_path = Path(out_dir) / f"{report['report_id']}.pdf"
        if not pdf_path.exists():
            raise FileNotFoundError(
                f"Missing report PDF {pdf_path} — run: "
                "python tests/evaluation/generate_synthea_reports.py")
        entries.append({
            "report_id": report["report_id"],
            "pdf_file": pdf_path.name,
            "layout": report.get("layout"),
            "observation_count": len(report["expected_observations"]),
            "size_bytes": pdf_path.stat().st_size,
            "sha256": sha256_of(pdf_path),
        })
    return {
        "generator": "tests/evaluation/pdf_fixtures.py (reportlab invariant mode)",
        "source_of_truth": "tests/evaluation/data/synthea_ground_truth.json",
        "deterministic": True,
        "reports": entries,
    }


