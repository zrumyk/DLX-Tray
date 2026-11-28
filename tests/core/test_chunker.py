# CJK punctuation, NBSP and curly quotes below are intentional test data.
# ruff: noqa: RUF001
"""Tests for the UTF-16-aware chunker."""

import sys
import time
import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dlx_tray.core.chunker import (
    DEFAULT_MAX_UNITS,
    HARD_MAX_UNITS,
    MIN_MAX_UNITS,
    join,
    split,
    utf16_len,
)
from dlx_tray.core.models import Chunk

EMOJI = "😀"  # U+1F600: one code point, a surrogate pair in UTF-16


def texts(chunks: list[Chunk]) -> list[str]:
    return [c.text for c in chunks]


def assert_valid(text: str, chunks: list[Chunk], max_units: int) -> None:
    """Every invariant of the chunker contract."""
    assert join(chunks, texts(chunks)) == text
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert utf16_len(chunk.text) <= max_units
        assert chunk.text == chunk.text.strip()
        assert chunk.separator.strip() == ""
    # Only a leading whitespace chunk may be empty.
    assert all(c.text for c in chunks[1:])


class TestUtf16Len:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("", 0), ("abc", 3), ("привіт", 6), ("中文", 2), (EMOJI, 2), ("a😀b", 4)],
    )
    def test_counts_code_units(self, text: str, expected: int) -> None:
        assert utf16_len(text) == expected

    def test_lone_surrogate_counts_one(self) -> None:
        assert utf16_len("\ud800") == 1


class TestValidation:
    @pytest.mark.parametrize("max_units", [-1, 0, 1, HARD_MAX_UNITS + 1])
    def test_rejects_out_of_range_max(self, max_units: int) -> None:
        with pytest.raises(ValueError, match="max_units"):
            split("hello", max_units)

    def test_defaults(self) -> None:
        assert DEFAULT_MAX_UNITS == 1500
        assert HARD_MAX_UNITS == 20_000
        split("x", HARD_MAX_UNITS)
        split("x", 2)

    def test_join_rejects_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="translations"):
            join([Chunk("a", "", 0)], [])


class TestEdgeCases:
    def test_empty(self) -> None:
        assert split("") == []
        assert join([], []) == ""

    @pytest.mark.parametrize("text", [" ", "   ", "\n\n", " \t\r\n "])
    def test_whitespace_only_is_one_untranslatable_chunk(self, text: str) -> None:
        chunks = split(text)
        assert chunks == [Chunk("", text, 0)]
        assert not any(c.translatable for c in chunks)
        assert join(chunks, [""]) == text

    def test_leading_and_trailing_whitespace_stay_local(self) -> None:
        chunks = split("  \n Hello world. \n")
        assert chunks == [Chunk("", "  \n ", 0), Chunk("Hello world.", " \n", 1)]

    def test_exactly_max_is_one_chunk(self) -> None:
        text = "a" * 10
        assert split(text, 10) == [Chunk(text, "", 0)]

    def test_max_plus_one_with_space_cuts_at_space(self) -> None:
        text = "aaaa bbbbbb"  # 11 units
        chunks = split(text, 10)
        assert texts(chunks) == ["aaaa", "bbbbbb"]
        assert chunks[0].separator == " "

    def test_max_plus_one_without_space_hard_cuts(self) -> None:
        chunks = split("a" * 11, 10)
        assert texts(chunks) == ["a" * 10, "a"]
        assert [c.separator for c in chunks] == ["", ""]

    def test_word_longer_than_max(self) -> None:
        text = "short " + "x" * 25 + " tail"
        chunks = split(text, 10)
        assert_valid(text, chunks, 10)
        assert texts(chunks) == ["short", "x" * 10, "x" * 10, "x" * 5 + " tail"]

    def test_no_spaces_at_all(self) -> None:
        text = "漢字" * 2000
        chunks = split(text, DEFAULT_MAX_UNITS)
        assert_valid(text, chunks, DEFAULT_MAX_UNITS)
        assert len(chunks) == 3
        assert all(c.separator == "" for c in chunks)

    def test_crlf_layout_is_preserved(self) -> None:
        text = "First line.\r\nSecond line.\r\n\r\nNext paragraph.\r\n"
        chunks = split(text, 30)
        assert_valid(text, chunks, 30)
        assert texts(chunks) == ["First line.\r\nSecond line.", "Next paragraph."]
        assert [c.separator for c in chunks] == ["\r\n\r\n", "\r\n"]

    def test_emoji_cost_two_units(self) -> None:
        text = EMOJI * 5  # 10 units, 5 code points
        assert split(text, 10) == [Chunk(text, "", 0)]
        chunks = split(text, 9)
        assert texts(chunks) == [EMOJI * 4, EMOJI]

    @pytest.mark.parametrize("max_units", [2, 3, 5, 7])
    def test_hard_cut_never_splits_a_surrogate_pair(self, max_units: int) -> None:
        text = ("a" + EMOJI * 3) * 4
        chunks = split(text, max_units)
        assert_valid(text, chunks, max_units)
        for chunk in chunks:
            chunk.text.encode("utf-16-le")  # raises on a lone surrogate

    def test_hard_cut_keeps_combining_marks_with_their_base(self) -> None:
        text = "e\u0301" * 20  # é as e + COMBINING ACUTE ACCENT
        chunks = split(text, 5)
        assert_valid(text, chunks, 5)
        assert not any(unicodedata.combining(c.text[0]) for c in chunks)

    def test_hard_cut_keeps_zwj_sequences_together(self) -> None:
        family = "👨\u200d👩\u200d👧"  # 8 units
        text = family * 3
        chunks = split(text, 10)
        assert_valid(text, chunks, 10)
        assert texts(chunks) == [family] * 3

    def test_cjk_punctuation_is_a_sentence_boundary(self) -> None:
        text = "今天天气很好。我们去公园吧！你觉得怎么样？"
        chunks = split(text, 15)
        assert_valid(text, chunks, 15)
        assert texts(chunks) == ["今天天气很好。我们去公园吧！", "你觉得怎么样？"]
        assert chunks[0].separator == ""

    def test_cjk_boundary_after_closing_bracket(self) -> None:
        text = "他说：「好的。」然后走了很远很远的路"
        chunks = split(text, 12)
        assert_valid(text, chunks, 12)
        assert chunks[0].text == "他说：「好的。」"

    def test_many_short_lines_are_merged(self) -> None:
        text = "\n".join(f"line {i:03}" for i in range(500))  # 8 units each + "\n"
        chunks = split(text, 100)
        assert_valid(text, chunks, 100)
        # 11 lines fit in 100 units (11 * 8 + 10 = 98), so the merge is as tight as possible.
        assert len(chunks) == -(-500 // 11)
        assert all(c.separator == "\n" for c in chunks[:-1])
        assert chunks[-1].separator == ""


class TestBoundaryPriority:
    def test_paragraph_beats_line(self) -> None:
        text = "Aaa.\n\nBbb.\nCcc. Ddd"
        chunks = split(text, 18)
        assert texts(chunks)[0] == "Aaa."

    def test_line_beats_sentence(self) -> None:
        text = "Aaa. Bbb\nCcc. Ddd eee"
        chunks = split(text, 16)
        assert texts(chunks)[0] == "Aaa. Bbb"

    def test_sentence_beats_space(self) -> None:
        text = "Aaa bbb. Ccc ddd eee"
        chunks = split(text, 16)
        assert texts(chunks)[0] == "Aaa bbb."

    def test_sentence_end_with_closing_quote(self) -> None:
        text = 'He said "stop." Then he left the room'
        chunks = split(text, 30)
        assert texts(chunks)[0] == 'He said "stop."'

    @pytest.mark.parametrize("mark", ["!", "?", "…"])
    def test_other_sentence_marks(self, mark: str) -> None:
        text = f"One two{mark} Three four five"
        assert texts(split(text, 20))[0] == f"One two{mark}"

    def test_latest_boundary_of_best_rank_wins(self) -> None:
        text = "P1.\n\nP2.\n\nP3 is long enough"
        chunks = split(text, 12)
        assert texts(chunks)[0] == "P1.\n\nP2."


class TestJoin:
    def test_translations_keep_the_layout(self) -> None:
        text = "  Hello.\n\nGood day.\r\nBye. "
        chunks = split(text, 10)
        translated = [c.text.upper() for c in chunks]
        assert join(chunks, translated) == "  HELLO.\n\nGOOD DAY.\r\nBYE. "


_ALPHABET = [
    "a", "b", "Ж", "ї", "中", "。", "！", EMOJI, "e\u0301", "\u200d",
    " ", " ", " ", "\t", "\n", "\n\n", "\r\n", "\u00a0",
    ".", "!", "?", "…", '"', "」",
]  # fmt: skip


_max_units = st.integers(min_value=MIN_MAX_UNITS, max_value=HARD_MAX_UNITS)
_small_max_units = st.integers(min_value=MIN_MAX_UNITS, max_value=40)


@pytest.mark.parametrize("max_units", [1_500, 12_000, 20_000])
@given(element=st.sampled_from(["a", "漢", "\U0001f600"]), extra=st.integers(1, 100))
def test_round_trip_at_provider_limits(max_units: int, element: str, extra: int) -> None:
    text = element * (max_units // utf16_len(element) + extra)
    chunks = split(text, max_units)
    assert len(chunks) == 2
    assert_valid(text, chunks, max_units)


_tricky_text = st.lists(st.sampled_from(_ALPHABET), max_size=400).map("".join)


@settings(max_examples=300)
@given(text=_tricky_text, max_units=_small_max_units)
def test_round_trip_invariant_tricky_text(text: str, max_units: int) -> None:
    assert_valid(text, split(text, max_units), max_units)


@given(text=st.text(), max_units=_max_units)
def test_round_trip_invariant_any_text(text: str, max_units: int) -> None:
    assert_valid(text, split(text, max_units), max_units)


@given(
    text=st.text(alphabet=st.characters(categories=["L", "Zs", "Cc", "P", "So"]), min_size=1),
    max_units=_small_max_units,
)
def test_join_places_translations_in_order(text: str, max_units: int) -> None:
    chunks = split(text, max_units)
    translated = [f"<{c.index}>" for c in chunks]
    expected = "".join(f"<{c.index}>{c.separator}" for c in chunks)
    assert join(chunks, translated) == expected


def test_large_text_round_trip_with_defaults() -> None:
    paragraph = "Це речення українською. This is an English sentence! 中文句子。 😀\n"
    text = (paragraph * 40 + "\n") * 50
    chunks = split(text)
    assert_valid(text, chunks, DEFAULT_MAX_UNITS)


# Coverage tracing slows pure-Python code several times over; the budget is checked without it.
@pytest.mark.skipif("coverage" in sys.modules, reason="timing is meaningless under coverage")
@pytest.mark.parametrize("with_emoji", [False, True])
def test_performance_100k_chars(with_emoji: bool) -> None:
    sentence = "The quick brown fox jumps over the lazy dog. " + (EMOJI if with_emoji else "")
    text = "\n\n".join([sentence * 20] * 200)[:100_000]
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        split(text)
        best = min(best, time.perf_counter() - started)
    assert best < 0.1, f"split took {best * 1000:.1f} ms"
