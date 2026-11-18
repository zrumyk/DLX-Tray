"""Load a file snapshot and export its translated text segments."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from docx import Document

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

    @property
    def output_suffixes(self) -> tuple[str, ...]:
        suffix = self.path.suffix.lower()
        return (".docx", ".txt") if suffix == ".pdf" else (suffix,)

    @classmethod
    def read(cls, path: Path) -> "SourceFile":
        match path.suffix.lower():
            case ".txt":
                content = read_txt(path)
                if not content.strip():
                    raise EmptyFileError
                return cls(path, (content,))
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
            write_txt(destination, translations[0])
