"""Check SQLite expiry, eviction and the data saved for each translation."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from dlx_tray.core.models import (
    AnalysisTier,
    Formality,
    GlossaryEntry,
    Language,
    NoteKind,
    PhilologicalNote,
    StylisticRegister,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.services import cache
from dlx_tray.services.cache import TranslationCache, translation_key


def test_cache_retains_only_the_digest_and_result_until_the_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 10**18
    monkeypatch.setattr(cache, "time_ns", lambda: now)
    path = tmp_path / "cache.db"
    marker = "unique source fixture\x00with a NUL"
    key = translation_key("dlx", Language.AUTO, Language.UK, Formality.DEFAULT, marker)
    result = TranslationResult("Переклад", Language.EN, "dlx")
    store = TranslationCache(path)
    assert store.lookup(key, "dlx") is None
    assert not path.exists()
    store.store(key, result)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM translations").fetchall() == [
            (key, result.text, "EN", now + cache.TTL_NS, now, None)
        ]
    assert marker.encode() not in path.read_bytes()
    restarted = TranslationCache(path)
    now += cache.TTL_NS - 1
    assert restarted.lookup(key, "dlx") == result
    now += 1
    assert restarted.lookup(key, "dlx") is None


def test_cache_evicts_the_least_recently_used_at_fifty_thousand(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    store = TranslationCache(path)
    old_key, evicted_key, new_key = b"old", b"evicted", b"new"
    result = TranslationResult("Result", None, "dlx")
    store.store(old_key, result)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE translations SET accessed = 0")
        db.execute(
            "INSERT INTO translations (key, translation, detected_source, expires, accessed) "
            "VALUES (?, 'Result', NULL, ?, 1)",
            (evicted_key, 2**62),
        )
        db.executemany(
            "INSERT INTO translations (key, translation, detected_source, expires, accessed) "
            "VALUES (?, 'Result', NULL, ?, ?)",
            ((i.to_bytes(4, "big"), 2**62, i + 2) for i in range(cache.MAX_ENTRIES - 2)),
        )
    assert store.lookup(old_key, "dlx") == result
    store.store(new_key, result)
    assert store.lookup(evicted_key, "dlx") is None
    assert store.lookup(old_key, "dlx") == result
    assert store.lookup(new_key, "dlx") == result
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM translations").fetchone() == (50_000,)
    store.clear()
    assert store.lookup(new_key, "dlx") is None


def test_cache_storage_failure_logs_no_text_or_digest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cache.db"
    path.write_bytes(b"not a database")
    store = TranslationCache(path)
    key = translation_key("dlx", Language.EN, Language.UK, Formality.DEFAULT, "source marker")
    assert store.lookup(key, "dlx") is None
    store.store(key, TranslationResult("translation marker", None, "dlx"))
    assert "Could not read translation cache" in caplog.text
    assert "Could not write translation cache" in caplog.text
    assert "marker" not in caplog.text
    assert key.hex() not in caplog.text


def test_v2_db_migration_keeps_legacy_hit_and_stores_notes(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    old_fields = ("dlx", "EN", "UK", "default", "source")
    old_key = sha256(
        b"".join(len(field).to_bytes(8, "big") + field.encode() for field in old_fields)
    ).digest()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE translations (key BLOB PRIMARY KEY, translation TEXT NOT NULL, "
            "detected_source TEXT, expires INTEGER NOT NULL, accessed INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO translations VALUES (?, 'Old', 'EN', ?, 0)", (old_key, 2**62))

    key = translation_key("dlx", Language.EN, Language.UK, Formality.DEFAULT, "source")
    assert key == old_key
    store = TranslationCache(path)
    assert store.lookup(key, "dlx") == TranslationResult("Old", Language.EN, "dlx")
    result = TranslationResult(
        "New", Language.EN, "cloud_ai", (PhilologicalNote(NoteKind.GRAMMAR, "Example note"),)
    )
    store.store(b"new", result)
    assert TranslationCache(path).lookup(b"new", "cloud_ai") == result
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute(
            "SELECT notes FROM translations WHERE key = ?", (old_key,)
        ).fetchone() == (None,)


def test_extended_cache_key_tracks_only_effective_options() -> None:
    options = TranslationOptions(
        style_context="  Modern \t prose ", glossary=(GlossaryEntry("Term", "Термін"),)
    )

    def key(opts: TranslationOptions, formality: Formality = Formality.DEFAULT) -> bytes:
        return translation_key("cloud_ai", Language.EN, Language.UK, formality, "TERM.", opts)

    baseline = key(options)
    equivalent = replace(
        options,
        style_context="Modern prose",
        glossary=(
            GlossaryEntry("unmatched", "unmatched"),
            GlossaryEntry("TERM", "Термін", priority=3),
        ),
    )
    assert key(equivalent, Formality.MORE) == baseline
    for changed in (
        replace(options, analysis_tier=AnalysisTier.DEEP),
        replace(options, register=StylisticRegister.ACADEMIC),
        replace(options, style_context="Historical prose"),
        replace(options, glossary=(GlossaryEntry("Term", "Поняття"),)),
        replace(options, include_notes=True),
    ):
        assert key(changed) != baseline
