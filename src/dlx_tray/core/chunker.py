"""Split texts into chunks that fit a UTF-16 budget and join translations back."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from itertools import accumulate

from dlx_tray.core.models import Chunk

DLX_MAX_UNITS = 1_500
HARD_MAX_UNITS = 20_000
DEFAULT_MAX_UNITS = DLX_MAX_UNITS
MIN_MAX_UNITS = 2  # every code point (an astral one costs 2 units) must fit into a chunk

_PARAGRAPH, _LINE, _SENTENCE, _SPACE = range(4)

_WHITESPACE = re.compile(r"\s+")
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")
_SENTENCE_END = frozenset(".!?…。！？")  # noqa: RUF001
_CLOSERS = frozenset("\"'”’»)]」』）")  # noqa: RUF001
# CJK sentence ends need no following space: the boundary sits right after them.
_CJK_SENTENCE_END = re.compile(r"[。！？][”’」』）]*(?=\S)")  # noqa: RUF001
_ZWJ = "\u200d"
_GRAPHEME_EXTENDERS = frozenset("\u200d\ufe0e\ufe0f") | {chr(c) for c in range(0x1F3FB, 0x1F400)}


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the way DLX counts it (astral characters cost 2)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def paragraphs(text: str) -> list[Chunk]:
    """Separate paragraphs without consuming their original blank-line separators."""
    pieces: list[Chunk] = []
    start = 0
    for match in _WHITESPACE.finditer(text):
        if match.start() > start and _line_break_count(match.group()) >= 2:
            pieces.append(Chunk(text[start : match.start()], match.group(), len(pieces)))
            start = match.end()
    if start < len(text):
        pieces.append(Chunk(text[start:], "", len(pieces)))
    return pieces


def split_segment(text: str, max_units: int) -> list[Chunk]:
    """Keep a segment intact unless its translatable text exceeds the request limit."""
    _validate_budget(max_units)
    content = text.strip()
    if utf16_len(content) > max_units:
        return split(text, max_units)
    start = len(text) - len(text.lstrip())
    pieces = [Chunk("", text[:start], 0)] if start else []
    if content:
        pieces.append(Chunk(content, text[start + len(content) :], len(pieces)))
    return pieces


def _validate_budget(max_units: int) -> None:
    if not MIN_MAX_UNITS <= max_units <= HARD_MAX_UNITS:
        msg = f"max_units must be in [{MIN_MAX_UNITS}, {HARD_MAX_UNITS}], got {max_units}"
        raise ValueError(msg)


def split(text: str, max_units: int = DEFAULT_MAX_UNITS) -> list[Chunk]:
    """Split ``text`` into chunks of at most ``max_units`` UTF-16 units each.

    Cuts at the best boundary that keeps a chunk within the limit, in priority order: blank
    line, line break, sentence end, whitespace, and a hard cut as the last resort. The
    whitespace at a cut becomes the chunk's ``separator``. Leading whitespace of the text
    becomes an empty, non-translatable first chunk, so ``join`` restores the text exactly.
    """
    _validate_budget(max_units)
    if not text:
        return []

    prefix = _unit_prefix(text)

    def units(start: int, end: int) -> int:
        return end - start if prefix is None else prefix[end] - prefix[start]

    def furthest_end(start: int) -> int:
        """The largest ``end`` such that ``text[start:end]`` fits into ``max_units``."""
        if prefix is None:
            return start + max_units
        return bisect_right(prefix, prefix[start] + max_units) - 1

    starts, ends = _boundaries(text)
    pieces: list[tuple[str, str]] = []
    content_end = len(text.rstrip())
    pos = len(text) - len(text.lstrip())
    if pos:
        pieces.append(("", text[:pos]))

    while pos < content_end:
        if units(pos, content_end) <= max_units:
            pieces.append((text[pos:content_end], text[content_end:]))
            break
        limit = furthest_end(pos)
        cut = _best_boundary(starts, ends, pos, limit)
        if cut is not None:
            start, end = cut
            pieces.append((text[pos:start], text[start:end]))
            pos = end
        else:
            cut_at = _hard_cut(text, pos, limit)
            pieces.append((text[pos:cut_at], ""))
            pos = cut_at

    return [Chunk(text=t, separator=s, index=i) for i, (t, s) in enumerate(pieces)]


def join(chunks: Sequence[Chunk], translations: Sequence[str]) -> str:
    """Re-assemble a text from per-chunk translations, restoring the original separators."""
    if len(chunks) != len(translations):
        msg = f"got {len(translations)} translations for {len(chunks)} chunks"
        raise ValueError(msg)
    return "".join(t + c.separator for c, t in zip(chunks, translations, strict=True))


def _unit_prefix(text: str) -> list[int] | None:
    """Cumulative UTF-16 lengths, or ``None`` when every character costs one unit."""
    if not _ASTRAL.search(text):
        return None
    return list(accumulate((2 if ord(c) > 0xFFFF else 1 for c in text), initial=0))


def _boundaries(text: str) -> tuple[list[list[int]], list[list[int]]]:
    """Candidate cut points per rank: separator start and end offsets, both sorted."""
    starts: list[list[int]] = [[], [], [], []]
    ends: list[list[int]] = [[], [], [], []]
    found: list[tuple[int, int, int]] = []
    for match in _WHITESPACE.finditer(text):
        start, end = match.span()
        if start == 0:
            continue
        found.append((start, end, _rank(text, match.group(), start)))
    for match in _CJK_SENTENCE_END.finditer(text):
        found.append((match.end(), match.end(), _SENTENCE))
    found.sort()
    for start, end, rank in found:
        starts[rank].append(start)
        ends[rank].append(end)
    return starts, ends


def _rank(text: str, run: str, start: int) -> int:
    line_breaks = _line_break_count(run)
    if line_breaks >= 2:
        return _PARAGRAPH
    if line_breaks == 1:
        return _LINE
    i = start - 1
    while i > 0 and text[i] in _CLOSERS:
        i -= 1
    return _SENTENCE if text[i] in _SENTENCE_END else _SPACE


def _line_break_count(run: str) -> int:
    return run.count("\n") + run.count("\r") - run.count("\r\n")


def _best_boundary(
    starts: list[list[int]], ends: list[list[int]], pos: int, limit: int
) -> tuple[int, int] | None:
    """The latest boundary of the best rank with ``pos < start <= limit``."""
    for rank_starts, rank_ends in zip(starts, ends, strict=True):
        i = bisect_right(rank_starts, limit) - 1
        if i >= 0 and rank_starts[i] > pos:
            return rank_starts[i], rank_ends[i]
    return None


def _hard_cut(text: str, pos: int, limit: int) -> int:
    """Cut at or before ``limit`` without splitting combining marks or emoji sequences."""
    cut = limit
    while cut > pos + 1 and (
        text[cut] in _GRAPHEME_EXTENDERS
        or text[cut - 1] == _ZWJ
        or unicodedata.combining(text[cut])
    ):
        cut -= 1
    return cut
