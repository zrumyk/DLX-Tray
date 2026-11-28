"""Cover glossary transactions, file exchange and block matching."""

from dataclasses import replace
from pathlib import Path

import pytest

from dlx_tray.core.models import GlossaryEntry, Language
from dlx_tray.services.glossary import Glossary, GlossaryFormatError, GlossaryMatcher, GlossaryStore

GLOSSARY = Glossary("Seminar", Language.EN, Language.UK, "Seminar terminology")


def test_glossary_crud(tmp_path: Path) -> None:
    path = tmp_path / "user" / "glossaries.sqlite3"
    store = GlossaryStore(path)
    old = GlossaryEntry("Cafe\u0301", "кафе", 12, "noun")
    saved = store.save(GLOSSARY, [old])
    assert saved.id is not None
    updated = replace(saved, name="  New name  ")
    second = GlossaryEntry("text", "текст")
    saved = store.save(updated, [old, second])
    assert saved.id is not None and saved.name == "New name"
    replacement = GlossaryEntry("CAFÉ", "кав'ярня", -2)
    store.merge(saved.id, [replacement])

    reopened = GlossaryStore(path)
    assert reopened.list() == (saved,)
    assert reopened.entries(saved.id) == (replacement, second)
    assert saved.supports(Language.EN, Language.UK)
    assert not saved.supports(Language.EN, Language.DE)
    assert replace(saved, source=Language.AUTO).supports(Language.FR, Language.UK)
    with pytest.raises(GlossaryFormatError):
        store.save(saved, [replacement, old])
    assert store.entries(saved.id) == (replacement, second)
    store.delete(saved.id)
    assert reopened.list() == ()
    with pytest.raises(GlossaryFormatError):
        store.entries(saved.id)
    with pytest.raises(GlossaryFormatError):
        store.merge(saved.id, [replacement])
    with pytest.raises(GlossaryFormatError):
        store.save(saved, [])
    assert store.save(GLOSSARY, []).id != saved.id


@pytest.mark.parametrize("suffix", [".csv", ".TSV"])
def test_utf8_round_trip(tmp_path: Path, suffix: str) -> None:
    store = GlossaryStore(tmp_path / "glossaries.sqlite3")
    entry = GlossaryEntry('назва, "цитата"\tterm', 'titre, "citation"', 3, "n.", "1\n2")
    glossary = Glossary("Пари", Language.UK, Language.FR)
    saved = store.save(glossary, [entry])
    assert saved.id is not None
    path = tmp_path / ("terms" + suffix)
    path.write_text("old", encoding="utf-8")
    store.export_file(saved.id, path)
    exported = path.read_bytes()
    assert "назва".encode() in exported
    path.write_bytes(b"\xef\xbb\xbf" + exported)
    imported = store.import_file(path, replace(glossary, name="Imported"))
    assert imported.id is not None and imported.id != saved.id
    assert store.entries(imported.id) == (entry,)


@pytest.mark.parametrize(
    ("suffix", "data"),
    [
        (".csv", b"source_term,target_term\nterm,value\nbad\n"),
        (".tsv", b'source_term\ttarget_term\nterm\tvalue\n"unterminated'),
        (".tsv", b"source_term\ttarget_term\nterm\tvalue\nTERM\tother"),
        (".tsv", b"source_term\ttarget_term\tpriority\nterm\tvalue\tbad"),
        (".csv", b"source_term,target_term,target_term\n"),
        (".csv", b"source_term,unknown\n"),
        (".csv", b"source_term,target_term\nterm,\xff"),
        (".csv", b"source_term,target_term\nterm, "),
        (".csv", b"source_term,target_term\nterm,\0"),
        (".csv", b"source_term,target_term,priority\nterm,value,9223372036854775808"),
        (".txt", b"source_term,target_term\nterm,value"),
    ],
)
def test_invalid_import_leaves_existing_store_intact(
    tmp_path: Path, suffix: str, data: bytes
) -> None:
    store = GlossaryStore(tmp_path / "glossaries.sqlite3")
    entry = GlossaryEntry("existing", "наявний")
    saved = store.save(GLOSSARY, [entry])
    assert saved.id is not None
    path = tmp_path / ("bad" + suffix)
    path.write_bytes(data)
    with pytest.raises(GlossaryFormatError):
        store.import_file(path, saved)
    assert store.list() == (saved,)
    assert store.entries(saved.id) == (entry,)


def test_failed_export_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GlossaryStore(tmp_path / "glossaries.sqlite3")
    saved = store.save(GLOSSARY, [])
    assert saved.id is not None
    path = tmp_path / "terms.csv"
    path.write_text("previous", encoding="utf-8")

    def fail(_source: Path, _destination: Path) -> Path:
        raise PermissionError("File open in another application")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(PermissionError):
        store.export_file(saved.id, path)
    assert path.read_text(encoding="utf-8") == "previous"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["glossaries.sqlite3", "terms.csv"]


def test_block_matches_union_and_caps_by_precedence() -> None:
    saved = [GlossaryEntry(f"term{i}", "saved", i) for i in range(155)]
    saved.append(GlossaryEntry("across segments", "not a match", 500))
    document = [GlossaryEntry("TERM0", "reviewed", -100), GlossaryEntry("café", "кав'ярня")]
    matcher = GlossaryMatcher(saved, document)
    segments = (
        " ".join(f"Term{i}" for i in range(80)) + " CAFE\u0301 across",
        "segments " + " ".join(f"term{i}" for i in range(80, 155)) + " caféine Term0",
    )
    matched = matcher.for_block(segments)
    assert len(matched) == 150
    assert [(entry.source_term, entry.target_term) for entry in matched[:3]] == [
        ("café", "кав'ярня"),
        ("term0", "reviewed"),
        ("term154", "saved"),
    ]
    assert matched[-1].source_term == "term7"
    assert matcher.for_block(reversed(segments)) == matched
    assert matcher.for_block(["term00 caféine"]) == ()
