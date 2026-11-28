"""Match canonical glossary pairs against individual source segments."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol
from unicodedata import category, normalize

MAX_BLOCK_TERMS = 150


class GlossaryPair(Protocol):
    @property
    def source_term(self) -> str: ...

    @property
    def target_term(self) -> str: ...

    @property
    def priority(self) -> int: ...


def _fold(text: str) -> str:
    return normalize("NFC", normalize("NFC", text).casefold())


def canonical_term(term: str) -> str:
    return _fold(term.strip())


def _word_character(char: str) -> bool:
    return char.isalnum() or char == "_" or category(char).startswith("M")


def _contains_term(text: str, term: str) -> bool:
    start = text.find(term)
    while start != -1:
        end = start + len(term)
        if (start == 0 or not _word_character(text[start - 1])) and (
            end == len(text) or not _word_character(text[end])
        ):
            return True
        start = text.find(term, start + 1)
    return False


def matching_terms(segments: Iterable[str], canonical_terms: Iterable[str]) -> Iterator[str]:
    """Find canonical terms in input order, with boundaries checked per segment."""
    folded = tuple(map(_fold, segments))
    for term in canonical_terms:
        if any(_contains_term(text, term) for text in folded):
            yield term


def matched_glossary(text: str, entries: Iterable[GlossaryPair]) -> tuple[tuple[str, str], ...]:
    """Return sorted folded-source/exact-target pairs, with higher priority winning ties.

    Equal-priority translations of the same term resolve lexicographically so reordering a
    glossary cannot change its meaning or its cache key. Distinct terms may overlap.
    """
    winners: dict[str, tuple[int, str]] = {}
    for entry in entries:
        term = canonical_term(entry.source_term)
        candidate = (-entry.priority, entry.target_term)
        if term and (term not in winners or candidate < winners[term]):
            winners[term] = candidate
    return tuple((term, winners[term][1]) for term in matching_terms((text,), sorted(winners)))
