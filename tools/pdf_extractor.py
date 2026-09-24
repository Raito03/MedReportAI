"""PDF → text extraction. Code-based, NOT an LLM call.

The LLM never sees raw PDF bytes — only the text output of this module.
"""

import pdfplumber


def pdf_to_text(file_path: str) -> str:
    """Extract readable text from a lab report PDF.

    Handles multi-column layouts by extracting text page-by-page.
    Returns the full concatenated text.
    """
    pages_text = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
    return "\n\n".join(pages_text)
