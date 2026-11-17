"""The chunker against the realistic texts in tests/fixtures/."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dlx_tray.core.chunker import DEFAULT_MAX_UNITS, HARD_MAX_UNITS, join, split, utf16_len
from tests.conftest import FIXTURE_FILES, read_fixture

SIZES = [50, 200, 700, DEFAULT_MAX_UNITS, HARD_MAX_UNITS]


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("max_units", SIZES)
def test_fixture_round_trips_within_limit(path: Path, max_units: int) -> None:
    text = read_fixture(path.name)
    chunks = split(text, max_units)
    assert join(chunks, [c.text for c in chunks]) == text
    assert all(utf16_len(c.text) <= max_units for c in chunks)
    assert all(c.text == c.text.strip() for c in chunks)


def test_fixtures_exist() -> None:
    names = {p.name for p in FIXTURE_FILES}
    assert {"uk_article.txt", "en_article.txt", "crlf_windows.txt", "emoji.txt"} <= names


@pytest.mark.parametrize("name", ["uk_article.txt", "en_article.txt"])
def test_articles_split_on_paragraphs_at_default_size(
    fixture_text: Callable[[str], str], name: str
) -> None:
    chunks = split(fixture_text(name))
    assert len(chunks) >= 2
    # Every cut between content chunks falls on a paragraph or line boundary.
    assert all("\n" in c.separator for c in chunks[:-1])


def test_long_sentence_is_cut_between_words(fixture_text: Callable[[str], str]) -> None:
    text = fixture_text("long_paragraph.txt")
    chunks = split(text)
    assert len(chunks) >= 2
    assert all(c.separator == " " for c in chunks[:-1])


def test_long_paragraph_prefers_sentence_ends(fixture_text: Callable[[str], str]) -> None:
    chunks = split(fixture_text("long_paragraph.txt"), 700)
    sentence_cuts = [c for c in chunks[:-1] if c.text.endswith((".", "!", "?", '."'))]
    assert sentence_cuts, "expected at least one cut at a sentence end"


def test_crlf_is_preserved_byte_for_byte(fixture_text: Callable[[str], str]) -> None:
    text = fixture_text("crlf_windows.txt")
    assert "\r\n" in text
    for max_units in (40, 120):
        chunks = split(text, max_units)
        restored = join(chunks, [c.text.upper() for c in chunks])
        assert restored.count("\r\n") == text.count("\r\n")


def test_emoji_sequences_are_not_broken(fixture_text: Callable[[str], str]) -> None:
    text = fixture_text("emoji.txt")
    for max_units in (20, 50):
        for chunk in split(text, max_units):
            assert not chunk.text.startswith("\u200d")
            assert not chunk.text.endswith("\u200d")
            assert not chunk.text.startswith(("\U0001f3fb", "\U0001f3fc", "\ufe0f"))


def test_cjk_cuts_after_sentence_marks(fixture_text: Callable[[str], str]) -> None:
    chunks = split(fixture_text("cjk.txt"), 60)
    ends = [c.text[-1] for c in chunks[:-1]]
    assert sum(e in "。！？」" for e in ends) >= len(ends) // 2  # noqa: RUF001
