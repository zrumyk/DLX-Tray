"""Plain-text import and export tests."""

from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from collections.abc import Callable
from pathlib import Path

import pytest

from dlx_tray.fileio.txt import read_txt, write_txt


@pytest.mark.parametrize(
    ("bom", "encoding"),
    [
        (b"", "utf-8"),
        (BOM_UTF8, "utf-8"),
        (BOM_UTF16_LE, "utf-16-le"),
        (BOM_UTF16_BE, "utf-16-be"),
        (BOM_UTF32_LE, "utf-32-le"),
        (BOM_UTF32_BE, "utf-32-be"),
        (b"", "cp1251"),
    ],
)
def test_read_detects_encoding_and_preserves_crlf(
    tmp_path: Path, fixture_text: Callable[[str], str], bom: bytes, encoding: str
) -> None:
    source = tmp_path / "джерело.txt"
    original = fixture_text("uk_article.txt").replace("\n", "\r\n")
    source.write_bytes(bom + original.encode(encoding))

    assert read_txt(source) == original


@pytest.mark.parametrize("contents", [b"", BOM_UTF8, BOM_UTF16_LE, BOM_UTF32_BE])
def test_read_empty_text(tmp_path: Path, contents: bytes) -> None:
    source = tmp_path / "empty.txt"
    source.write_bytes(contents)

    assert read_txt(source) == ""


def test_only_the_initial_utf8_bom_is_removed(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(BOM_UTF8 + b"first" + BOM_UTF8 + b"second")

    assert read_txt(source) == "first\ufeffsecond"


def test_invalid_bom_marked_input_fails_without_guessing(tmp_path: Path) -> None:
    source = tmp_path / "invalid.txt"
    source.write_bytes(BOM_UTF16_LE + b"a")

    with pytest.raises(UnicodeDecodeError):
        read_txt(source)


def test_read_rejects_unrecognized_encoding(tmp_path: Path) -> None:
    source = tmp_path / "unknown.txt"
    source.write_bytes(b"\x00\xff" * 16)

    with pytest.raises(ValueError, match="Could not determine the text file encoding"):
        read_txt(source)


def test_write_replaces_existing_file_with_exact_utf8_bytes(
    tmp_path: Path, fixture_text: Callable[[str], str]
) -> None:
    destination = tmp_path / "переклад.txt"
    destination.write_bytes(b"Existing text")
    translation = fixture_text("emoji.txt") + "Привіт\r\n\r\nHello\rBye\n"

    write_txt(destination, translation)

    assert destination.read_bytes() == translation.encode("utf-8")
    assert read_txt(destination) == translation
    assert list(tmp_path.iterdir()) == [destination]


def test_write_creates_an_empty_file(tmp_path: Path) -> None:
    destination = tmp_path / "empty.txt"

    write_txt(destination, "")

    assert destination.read_bytes() == b""


def test_failed_write_keeps_existing_contents_and_cleans_up(tmp_path: Path) -> None:
    destination = tmp_path / "existing.txt"
    destination.write_bytes(b"Existing text")

    with pytest.raises(UnicodeEncodeError):
        write_txt(destination, "Invalid surrogate: \ud800")

    assert destination.read_bytes() == b"Existing text"
    assert list(tmp_path.iterdir()) == [destination]
