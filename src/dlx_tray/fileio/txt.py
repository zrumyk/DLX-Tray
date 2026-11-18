"""Read and write plain-text documents without changing their line endings."""

from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from pathlib import Path
from tempfile import NamedTemporaryFile

from charset_normalizer import from_bytes

_UNMARKED_UNICODE = {"utf_16_le", "utf_16_be", "utf_32_le", "utf_32_be"}


def read_txt(path: Path) -> str:
    """Read Unicode text or infer its encoding, raising ValueError if decoding fails."""
    data = path.read_bytes()
    # A little-endian UTF-32 BOM starts with the same bytes as a UTF-16 BOM.
    if data.startswith((BOM_UTF32_LE, BOM_UTF32_BE)):
        return data.decode("utf-32")
    if data.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
        return data.decode("utf-16")
    if data.startswith(BOM_UTF8):
        return data.decode("utf-8-sig")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    # ASCII in unmarked UTF-16 or UTF-32 can also decode as UTF-8 with NULs.
    if text is not None and "\x00" not in text:
        return text

    match = from_bytes(data, enable_fallback=False).best()
    if match is not None and (text is None or match.encoding in _UNMARKED_UNICODE):
        return data.decode(match.encoding)
    if text is not None:
        return text

    raise ValueError("Could not determine the text file encoding.")


def write_txt(path: Path, text: str) -> None:
    """Atomically replace the destination with UTF-8 text without a BOM."""
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
