"""Reconstruct paragraphs from a PDF text layer and export their translations."""

from collections.abc import Callable
from pathlib import Path

import pdfplumber
from docx import Document

from .docx_io import _save_docx
from .txt import write_txt


def read_pdf(path: Path) -> list[str]:
    """Return reconstructed paragraphs in page order, or raise if there is no text layer."""
    paragraphs: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = page.extract_text_lines(return_chars=False)
            page.close()
            paragraph: list[str] = []
            previous_bottom = previous_left = previous_height = 0.0
            for line in lines:
                text = line["text"].strip()
                if not text:
                    continue
                top, bottom, left = line["top"], line["bottom"], line["x0"]
                height = bottom - top
                smaller_height = min(previous_height, height)
                # Compare against glyph height so the same thresholds scale with font size.
                # Only an inward shift starts a paragraph: its next line may return to the margin.
                if paragraph and (
                    top - previous_bottom > 0.6 * smaller_height
                    or left - previous_left > 0.5 * smaller_height
                    or abs(height - previous_height) > 0.35 * smaller_height
                ):
                    paragraphs.append(" ".join(paragraph))
                    paragraph.clear()
                paragraph.append(text)
                previous_bottom, previous_left, previous_height = bottom, left, height
            if paragraph:
                paragraphs.append(" ".join(paragraph))

    if not paragraphs:
        raise ValueError("The PDF has no extractable text layer.")
    return paragraphs


def translate_pdf(source: Path, destination: Path, translate: Callable[[str], str]) -> None:
    """Translate each paragraph with a synchronous callback and atomically save DOCX or TXT."""
    suffix = destination.suffix.lower()
    if suffix not in {".docx", ".txt"}:
        raise ValueError("PDF translations must be saved as .docx or .txt.")

    paragraphs = [translate(paragraph) for paragraph in read_pdf(source)]
    if suffix == ".txt":
        write_txt(destination, "\n\n".join(paragraphs))
        return

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    _save_docx(document, destination)
