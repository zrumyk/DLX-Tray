"""Check file snapshot and export validation independently of Qt."""

from pathlib import Path

import pytest
from docx import Document

from dlx_tray.fileio.translation import EmptyFileError, SourceFile, SourceOverwriteError


@pytest.mark.parametrize("suffix", [".txt", ".docx"])
def test_no_translatable_text(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"empty{suffix}"
    if suffix == ".docx":
        Document().save(str(path))
    else:
        path.write_bytes(b"\r\n \t\r\n")
    with pytest.raises(EmptyFileError):
        SourceFile.read(path)


def test_rejects_unsupported_formats_and_missing_translations(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        SourceFile.read(tmp_path / "image.png")

    path = tmp_path / "source.txt"
    path.write_bytes(b"Original")
    source = SourceFile.read(path)
    destination = tmp_path / "output.txt"
    with pytest.raises(ValueError, match="Invalid"):
        source.save(destination, ())
    with pytest.raises(ValueError, match="Invalid"):
        source.save(destination.with_suffix(".pdf"), ("Translation",))
    assert not destination.exists()


def test_cannot_export_to_an_alias_of_the_source(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"Original")
    alias = tmp_path / "alias.txt"
    alias.hardlink_to(path)
    with pytest.raises(SourceOverwriteError):
        SourceFile.read(path).save(alias, ("Translation",))
    assert alias.read_bytes() == path.read_bytes() == b"Original"


def test_export_uses_original_text_snapshot_after_source_disappears(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"Original")
    source = SourceFile.read(path)
    path.unlink()
    assert source.segments == ("Original",)
    with pytest.raises(SourceOverwriteError):
        source.save(path, ("Translation",))
    destination = tmp_path / "translation.txt"
    source.save(destination, ("Translation",))
    assert destination.read_bytes() == b"Translation"


def test_txt_snapshot_preserves_mixed_paragraph_separators(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    original = b"\r\n  First\r\nwrapped\r\n \t\r\n  Second\r\rThird\n "
    path.write_bytes(original)
    source = SourceFile.read(path)

    assert source.segments == ("First\r\nwrapped", "Second", "Third")
    assert source.assemble(source.segments).encode() == original
    assert source.assemble(("One",), partial="T") == "\r\n  One\r\n \t\r\n  T"
    destination = tmp_path / "translated.txt"
    source.save(destination, ("One\r\ncontinued", "Two", "Three"))
    assert destination.read_bytes() == b"\r\n  One\r\ncontinued\r\n \t\r\n  Two\r\rThree\n "
