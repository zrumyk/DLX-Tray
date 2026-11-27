"""Persistent cache for complete single-provider translations."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import closing, contextmanager
from time import time_ns
from typing import TYPE_CHECKING

from dlx_tray.core.models import Language, TranslationResult

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dlx_tray.core.models import Formality

log = logging.getLogger(__name__)
MAX_ENTRIES = 50_000
TTL_NS = 30 * 24 * 60 * 60 * 10**9


def translation_key(
    provider: str, source: Language, target: Language, formality: Formality, text: str
) -> bytes:
    digest = hashlib.sha256()
    for field in (provider, source.value, target.value, formality.value, text):
        encoded = field.encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=1.0)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    key BLOB PRIMARY KEY,
                    translation TEXT NOT NULL,
                    detected_source TEXT,
                    expires INTEGER NOT NULL,
                    accessed INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS translations_accessed ON translations (accessed)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS translations_expires ON translations (expires)"
            )
            yield connection

    def lookup(self, key: bytes, provider: str) -> TranslationResult | None:
        try:
            if not self._path.exists():
                return None
            now = time_ns()
            with self._connection() as connection:
                row = connection.execute(
                    """
                    UPDATE translations SET accessed = max(
                        ?, (SELECT max(accessed) FROM translations) + 1
                    )
                    WHERE key = ? AND expires > ?
                    RETURNING translation, detected_source
                    """,
                    (now, key, now),
                ).fetchone()
                if row is not None:
                    text, detected = row
                    return TranslationResult(
                        text, Language(detected) if detected else None, provider
                    )
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.warning("Could not read translation cache (%s)", type(exc).__name__)
        return None

    def store(self, key: bytes, result: TranslationResult) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            now = time_ns()
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO translations VALUES (?, ?, ?, ?, max(
                        ?, coalesce((SELECT max(accessed) FROM translations) + 1, 0)
                    ))
                    ON CONFLICT(key) DO UPDATE SET
                        translation = excluded.translation,
                        detected_source = excluded.detected_source,
                        expires = excluded.expires, accessed = excluded.accessed
                    """,
                    (key, result.text, result.detected_source, now + TTL_NS, now),
                )
                connection.execute("DELETE FROM translations WHERE expires <= ?", (now,))
                connection.execute(
                    """
                    DELETE FROM translations WHERE accessed < (
                        SELECT accessed FROM translations ORDER BY accessed DESC LIMIT 1 OFFSET ?
                    )
                    """,
                    (MAX_ENTRIES - 1,),
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.warning("Could not write translation cache (%s)", type(exc).__name__)

    def clear(self) -> None:
        if self._path.exists():
            with self._connection() as connection:
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("DELETE FROM translations")
