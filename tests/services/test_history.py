"""Exercise the history service against on-disk SQLite databases."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC
from pathlib import Path

import pytest

from dlx_tray.core.models import (
    GlossaryEntry,
    Language,
    NoteKind,
    PhilologicalNote,
    SegmentAnnotation,
    StylisticRegister,
    TranslationAnnotations,
)
from dlx_tray.services.history import HistoryStore


def _add(store: HistoryStore, source: str, target: str = "") -> None:
    store.add(
        source_lang=Language.AUTO,
        target_lang=Language.UK,
        source_text=source,
        target_text=target,
        provider="dlx",
    )


def test_migrates_v2_application_history_without_changing_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    original = (
        41,
        "2026-11-19T04:01:02+00:00",
        "auto",
        "UK",
        "Old\r\ntext",
        "Текст",
        "x.txt",
        "dlx",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """CREATE TABLE history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                source_lang TEXT NOT NULL, target_lang TEXT NOT NULL, source_text TEXT NOT NULL,
                target_text TEXT NOT NULL, file_name TEXT, provider TEXT NOT NULL
            )"""
        )
        connection.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?)", original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: HistoryStore(path, 5), range(4)))
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT * FROM history").fetchone() == (*original, None)
    (old,) = stores[0].search("OLD")
    assert old.id == 41 and old.annotations is None
    assert old.created_at.isoformat() == original[1]

    annotations = TranslationAnnotations(
        StylisticRegister.LITERARY,
        (
            SegmentAnnotation(
                0,
                0,
                ("cloud_ai",),
                (GlossaryEntry("Oxford", "Оксфорд"),),
                (PhilologicalNote(NoteKind.GRAMMAR, "Узгодження"),),
            ),
        ),
    )
    stores[0].add(
        source_lang=Language.EN,
        target_lang=Language.UK,
        source_text="Oxford",
        target_text="Оксфорд",
        provider="cloud_ai",
        annotations=annotations,
    )
    new, unchanged = HistoryStore(path, 5).list()
    assert new.id == 42 and new.annotations == annotations
    assert unchanged == old


def test_does_not_modify_newer_history_schema(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(ValueError, match="Unsupported translation history schema"):
        HistoryStore(path, 5)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)


def test_persists_translation_metadata(tmp_path: Path) -> None:
    path = tmp_path / "data" / "history.sqlite3"
    store = HistoryStore(path, 5)
    assert store.list() == []
    _add(store, "An ordinary translation", "Звичайний переклад")
    store.add(
        source_lang=Language.UK,
        target_lang=Language.EN_GB,
        source_text="Line 1\r\nLine 2",
        target_text="First line\r\nSecond line",
        provider="deepl",
        file_name="Нотатки.txt",
    )

    file_entry, text_entry = HistoryStore(path, 5).list()
    assert file_entry.id > text_entry.id
    assert file_entry.created_at.tzinfo is UTC
    assert file_entry.source_lang is Language.UK
    assert file_entry.target_lang is Language.EN_GB
    assert file_entry.source_text == "Line 1\r\nLine 2"
    assert file_entry.target_text == "First line\r\nSecond line"
    assert file_entry.provider == "deepl"
    assert file_entry.file_name == "Нотатки.txt"
    assert text_entry.source_lang is Language.AUTO
    assert text_entry.file_name is None


def test_insert_enforces_current_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, 2)
    for source in ("first", "second", "third"):
        _add(store, source)
    assert [entry.source_text for entry in store.list()] == ["third", "second"]

    smaller_store = HistoryStore(path, 1)
    _add(smaller_store, "fourth")
    assert [entry.source_text for entry in smaller_store.list()] == ["fourth"]


def test_rejects_nonpositive_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with pytest.raises(ValueError, match="positive"):
        HistoryStore(path, 0)
    assert not path.exists()


def test_accepts_limit_above_sqlite_integer_range(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", 2**70)
    _add(store, "first")
    assert [entry.source_text for entry in store.list()] == ["first"]


def test_searches_both_texts_case_insensitively_in_insertion_order(
    tmp_path: Path, fixture_text: Callable[[str], str]
) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", 5)
    article = fixture_text("uk_article.txt")
    _add(store, article, "Machine translation")
    _add(store, "Music", "Музика")
    _add(store, "Translated text", "Машинний переклад")

    assert [entry.source_text for entry in store.search("ашиннИЙ переклад")] == [
        "Translated text",
        article,
    ]
    assert len(store.search("MACHINE")) == 1
    assert store.search("") == store.list()


@pytest.mark.parametrize("literal", ["%", "_", "!", "'"])
def test_search_treats_pattern_characters_literally(tmp_path: Path, literal: str) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", 5)
    _add(store, f"x{literal}y")
    _add(store, "xzy")
    assert [entry.source_text for entry in store.search(f"x{literal}y")] == [f"x{literal}y"]
    assert store.search("' OR 1=1 --") == []


def test_search_handles_nul_in_text_and_query(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", 5)
    _add(store, "A\0Ї")
    _add(store, "AB")
    assert [entry.source_text for entry in store.search("ї")] == ["A\0Ї"]
    assert [entry.source_text for entry in store.search("a\0ї")] == ["A\0Ї"]


def test_search_handles_query_larger_than_sqlite_like_limit(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3", 5)
    _add(store, "long", "Ї" * 50_000)
    _add(store, "short", "Ї")
    assert [entry.source_text for entry in store.search("ї" * 50_000)] == ["long"]


def test_delete_and_clear_persist_without_reusing_ids(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, 5)
    _add(store, "first")
    _add(store, "second")
    latest, earliest = store.list()

    store.delete(earliest.id)
    store.delete(earliest.id)
    assert HistoryStore(path, 5).list() == [latest]
    store.clear()
    assert HistoryStore(path, 5).list() == []

    _add(store, "third")
    assert store.list()[0].id > latest.id


def test_rolls_back_insert_when_pruning_fails(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, 1)
    _add(store, "existing")
    with closing(sqlite3.connect(path, autocommit=False)) as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER prevent_prune BEFORE DELETE ON history
            BEGIN
                SELECT RAISE(ABORT, 'prune failed');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="prune failed"):
        _add(store, "new")
    assert [entry.source_text for entry in store.list()] == ["existing"]


def test_concurrent_inserts_enforce_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"

    def add(number: int) -> None:
        _add(HistoryStore(path, 5), str(number))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(add, range(20)))

    entries = HistoryStore(path, 5).list()
    assert [entry.id for entry in entries] == [20, 19, 18, 17, 16]
    assert len({entry.source_text for entry in entries}) == 5
