"""Load a file snapshot and export its translated text segments."""

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from docx import Document

from dlx_tray.core.chunker import paragraphs

from .docx_io import _save_docx, _translate_document
from .pdf_io import read_pdf, write_pdf_translation
from .txt import read_txt, write_txt

FILE_SUFFIXES = (".txt", ".docx", ".pdf")


class EmptyFileError(ValueError):
    """The file has no translatable text."""


class SourceOverwriteError(ValueError):
    """The destination refers to the input file."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    segments: tuple[str, ...]
    docx_package: bytes | None = None
    txt_prefix: str = ""
    txt_separators: tuple[str, ...] | None = None

    @property
    def output_suffixes(self) -> tuple[str, ...]:
        suffix = self.path.suffix.lower()
        return (".docx", ".txt") if suffix == ".pdf" else (suffix,)

    @classmethod
    def read(cls, path: Path) -> "SourceFile":
        match path.suffix.lower():
            case ".txt":
                content = read_txt(path)
                body = content.lstrip()
                text = body.rstrip()
                if not text:
                    raise EmptyFileError
                pieces = paragraphs(text)
                return cls(
                    path,
                    tuple(piece.text for piece in pieces),
                    txt_prefix=content[: len(content) - len(body)],
                    txt_separators=(*(piece.separator for piece in pieces[:-1]), body[len(text) :]),
                )
            case ".pdf":
                return cls(path, tuple(read_pdf(path)))
            case ".docx":
                package = path.read_bytes()
                segments: list[str] = []

                def collect(text: str) -> str:
                    segments.append(text)
                    return text

                _translate_document(Document(BytesIO(package)), collect)
                if not segments:
                    raise EmptyFileError
                return cls(path, tuple(segments), package)
            case _:
                raise ValueError("Unsupported input file format.")

    def assemble(self, segments: Sequence[str], *, partial: str = "") -> str:
        """Render the source, translated file, or translated prefix in document order."""
        if self.txt_separators is None:
            return "\n\n".join([*segments, partial] if partial else segments)
        if not segments and not partial:
            return ""
        completed = "".join(
            text + separator for text, separator in zip(segments, self.txt_separators, strict=False)
        )
        return self.txt_prefix + completed + partial

    def save(self, destination: Path, translations: tuple[str, ...]) -> None:
        suffix = destination.suffix.lower()
        if suffix not in self.output_suffixes or len(translations) != len(self.segments):
            raise ValueError("Invalid file translation export.")
        try:
            same_file = destination.samefile(self.path)
        except FileNotFoundError:
            same_file = False
        if same_file or destination.resolve() == self.path.resolve():
            raise SourceOverwriteError

        if self.docx_package is not None:
            document = Document(BytesIO(self.docx_package))
            replacements = iter(translations)
            _translate_document(document, lambda _: next(replacements))
            _save_docx(document, destination)
        elif self.path.suffix.lower() == ".pdf":
            write_pdf_translation(destination, translations)
        else:
            write_txt(destination, self.assemble(translations))
