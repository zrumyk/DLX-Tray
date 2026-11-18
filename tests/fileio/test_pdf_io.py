"""Exercise PDF paragraph reconstruction and both translation exports."""

from pathlib import Path

import pytest
from docx import Document

from dlx_tray.fileio.pdf_io import read_pdf, translate_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE = FIXTURES / "field_notes.pdf"


def test_read_pdf_reconstructs_paragraphs_in_page_order() -> None:
    expected = []
    for page in range(1, 6):
        expected.extend(
            [
                f"Field notes {page}",
                "We walked the north trail just after sunrise. The first marker was "
                "hidden by tall grass. We cleared the path and recorded its position.",
                "The second marker stood beside a shallow creek. We took a bearing "
                "from the bridge and checked it against the map.",
                "The return route passed through a stand of birch trees.",
            ]
        )

    assert read_pdf(SOURCE) == expected


def test_read_pdf_reports_missing_text_layer() -> None:
    with pytest.raises(ValueError, match="no extractable text layer"):
        read_pdf(FIXTURES / "scanned_field_notes.pdf")


def _read_export(path: Path) -> list[str]:
    if path.suffix.lower() == ".docx":
        return [paragraph.text for paragraph in Document(str(path)).paragraphs]
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    return data.decode("utf-8").split("\n\n")


@pytest.mark.parametrize("suffix", [".DOCX", ".txt"])
def test_translate_pdf_exports_each_paragraph(tmp_path: Path, suffix: str) -> None:
    destination = tmp_path / f"translated{suffix}"
    received: list[str] = []

    def translate(paragraph: str) -> str:
        received.append(paragraph)
        return f"Переклад {len(received)}"

    translate_pdf(SOURCE, destination, translate)

    assert received == read_pdf(SOURCE)
    assert _read_export(destination) == [f"Переклад {number}" for number in range(1, 21)]


@pytest.mark.parametrize("suffix", [".docx", ".txt"])
def test_callback_failure_keeps_existing_output(tmp_path: Path, suffix: str) -> None:
    destination = tmp_path / f"translated{suffix}"
    destination.write_bytes(b"previous output")

    def translate(paragraph: str) -> str:
        if paragraph.startswith("The second marker"):
            raise RuntimeError("Translation interrupted")
        return paragraph.upper()

    with pytest.raises(RuntimeError, match="interrupted"):
        translate_pdf(SOURCE, destination, translate)
    assert destination.read_bytes() == b"previous output"
    assert list(tmp_path.iterdir()) == [destination]


def test_translate_pdf_rejects_unsupported_format_before_opening(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.docx or \.txt"):
        translate_pdf(tmp_path / "missing.pdf", tmp_path / "output.pdf", str.upper)
