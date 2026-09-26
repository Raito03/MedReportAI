"""PDF → text extraction. Code-based, NOT an LLM call.

The LLM never sees raw PDF bytes — only the text output of this module.

P0-T3 (PDF extraction robustness): extraction failures are *controlled* —
they are reported through a small ``ValueError``-based exception hierarchy
(matching the project's existing error convention) instead of crashing with
an uncontrolled traceback or silently returning misleading text:

- ``PdfExtractionError`` — base class for all controlled extraction failures.
- ``PdfInvalidError``    — the file cannot be read as a PDF (corrupt/malformed).
- ``PdfNoTextError``     — readable PDF but no extractable text
                           (``reason`` is ``"blank"`` or ``"image_only"``).

OCR is deliberately out of scope: image-only/scanned PDFs raise
``PdfNoTextError(reason="image_only")`` rather than fabricating text.
"""

import pdfplumber


class PdfExtractionError(ValueError):
    """Controlled PDF extraction failure.

    Subclasses ``ValueError`` to match the project's existing error-handling
    convention (orchestrator/agents raise ``ValueError`` for failures), so
    existing callers that catch ``ValueError`` keep working.
    """


class PdfInvalidError(PdfExtractionError):
    """The file could not be read as a PDF (corrupt, truncated, or not a PDF)."""


class PdfNoTextError(PdfExtractionError):
    """A readable PDF with no extractable text.

    ``reason`` distinguishes a genuinely blank/empty document ("blank") from
    an image-only/scanned document ("image_only") for which OCR would be
    required — OCR is out of scope for this project.
    """

    def __init__(self, message: str, reason: str = "blank"):
        super().__init__(message)
        self.reason = reason  # "blank" | "image_only"


def _page_has_images(page) -> bool:
    """Best-effort check for embedded images on a page (image-only detection)."""
    try:
        return len(page.images) > 0
    except Exception:
        return False


def pdf_to_text(file_path: str) -> str:
    """Extract readable text from a lab report PDF.

    Pages are processed in order and concatenated deterministically with a
    blank line between pages. Pages without text are skipped without losing
    the text of other pages.

    Returns:
        The concatenated text of all pages that contain extractable text.

    Raises:
        FileNotFoundError: the path does not exist (unchanged behavior).
        PdfInvalidError: the file cannot be read as a PDF (corrupt/malformed).
        PdfNoTextError: readable PDF but no extractable text — either a blank
            document (``reason="blank"``) or an image-only/scanned document
            (``reason="image_only"``; OCR is out of scope, so text extraction
            is unsupported for this case).
        PdfExtractionError: any other extraction/read failure (e.g. a page
            that fails while reading).
    """
    try:
        pdf = pdfplumber.open(file_path)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise PdfInvalidError(
            f"Cannot read PDF — file is corrupt or not a valid PDF: "
            f"{file_path} ({type(exc).__name__}: {exc})"
        ) from exc

    pages_text = []
    saw_image = False
    try:
        with pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text()
                except Exception as exc:
                    raise PdfExtractionError(
                        f"Failed to read page {page_number} of PDF: "
                        f"{file_path} ({type(exc).__name__}: {exc})"
                    ) from exc
                if _page_has_images(page):
                    saw_image = True
                if text and text.strip():
                    pages_text.append(text)
    except PdfExtractionError:
        raise
    except Exception as exc:
        raise PdfInvalidError(
            f"Cannot read PDF — file is corrupt or not a valid PDF: "
            f"{file_path} ({type(exc).__name__}: {exc})"
        ) from exc

    if not pages_text:
        if saw_image:
            raise PdfNoTextError(
                "No extractable text found — the PDF appears to be an "
                "image-only/scanned document. Text extraction without OCR is "
                f"unsupported (OCR is out of scope): {file_path}",
                reason="image_only",
            )
        raise PdfNoTextError(
            f"No extractable text found — the PDF is blank or empty: {file_path}",
            reason="blank",
        )

    return "\n\n".join(pages_text)

