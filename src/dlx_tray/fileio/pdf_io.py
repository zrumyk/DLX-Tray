"""Reconstruct paragraphs from a PDF text layer and export their translations."""

from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median_low

import pdfplumber
from docx import Document

from .docx_io import _save_docx
from .txt import write_txt


class NoTextLayerError(ValueError):
    """The PDF contains no extractable text."""


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    left: float
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


def read_pdf(path: Path) -> list[str]:
    """Return reconstructed paragraphs in page order, or raise if there is no text layer."""
    paragraphs: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = [
                _Line(text, line["x0"], line["top"], line["bottom"])
                for line in page.extract_text_lines(return_chars=False)
                if (text := line["text"].strip())
            ]
            page.close()
            paragraphs.extend(_page_paragraphs(lines))

    if not paragraphs:
        raise NoTextLayerError("The PDF has no extractable text layer.")
    return paragraphs


def _page_paragraphs(lines: list[_Line]) -> list[str]:
    if not lines:
        return []

    # Same-size, aligned lines give an estimate of regular spacing, including double spacing.
    # Ignore larger gaps and use the lower median to limit the influence of paragraph breaks.
    gaps: list[float] = []
    for previous, current in pairwise(lines):
        height = min(previous.height, current.height)
        gap = current.top - previous.bottom
        if (
            0 <= gap <= 1.25 * height
            and height > 0
            and abs(current.left - previous.left) <= 0.5 * height
            and abs(current.height - previous.height) <= 0.35 * height
        ):
            gaps.append(gap / height)
    gap_limit = max(0.6, median_low(gaps) + 0.25) if len(gaps) >= 2 else 0.6

    paragraphs: list[str] = []
    paragraph = [lines[0].text]
    for previous, current in pairwise(lines):
        height = min(previous.height, current.height)
        # An inward shift can start a paragraph. Its following line may return to the margin.
        if (
            current.top - previous.bottom > gap_limit * height
            or current.left - previous.left > 0.5 * height
            or abs(current.height - previous.height) > 0.35 * height
        ):
            paragraphs.append(" ".join(paragraph))
            paragraph.clear()
        paragraph.append(current.text)
    paragraphs.append(" ".join(paragraph))
    return paragraphs


def translate_pdf(source: Path, destination: Path, translate: Callable[[str], str]) -> None:
    """Translate each paragraph with a synchronous callback and atomically save DOCX or TXT."""
    suffix = destination.suffix.lower()
    if suffix not in {".docx", ".txt"}:
        raise ValueError("PDF translations must be saved as .docx or .txt.")

    write_pdf_translation(destination, [translate(paragraph) for paragraph in read_pdf(source)])


def write_pdf_translation(destination: Path, paragraphs: list[str] | tuple[str, ...]) -> None:
    """Export already translated PDF paragraphs to DOCX or TXT."""
    suffix = destination.suffix.lower()
    if suffix == ".txt":
        write_txt(destination, "\n\n".join(paragraphs))
        return
    if suffix != ".docx":
        raise ValueError("PDF translations must be saved as .docx or .txt.")

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    _save_docx(document, destination)
