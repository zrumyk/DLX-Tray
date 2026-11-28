"""Exercise Unicode term boundaries and deterministic glossary priority."""

from dlx_tray.contracts.cloud_v1 import GlossaryTerm
from dlx_tray.contracts.glossary_match import matched_glossary
from dlx_tray.core.models import GlossaryEntry


def test_matches_whole_folded_terms_with_priority() -> None:
    entries = (
        GlossaryEntry("кіт", "cat"),
        GlossaryEntry("кот".upper(), "lower priority"),
        GlossaryEntry("кот", "cat", priority=1),
        GlossaryEntry("Кот", "tomcat", priority=1),
        GlossaryEntry("Straße", "street"),
        GlossaryEntry("café", "coffeehouse"),
        GlossaryEntry("bar", "bar"),
        GlossaryEntry("", "blank"),
    )
    text = f"кіт2, підкіт, коти, {'кот'.upper()}, STRASSE, cafe\u0301, foobar, bar_1."  # noqa: RUF001
    expected = (("café", "coffeehouse"), ("strasse", "street"), ("кот", "cat"))
    assert matched_glossary(text, entries) == expected
    assert matched_glossary(text, reversed(entries)) == expected


def test_combining_mark_still_counts_toward_word_boundary() -> None:
    assert matched_glossary("x\u0301", [GlossaryEntry("x", "letter")]) == ()


def test_wire_glossary_matches_like_domain_glossary() -> None:
    entries = [GlossaryTerm(source_term="Term", target_term="Equivalent")]
    assert matched_glossary("term.", entries) == (("term", "Equivalent"),)
