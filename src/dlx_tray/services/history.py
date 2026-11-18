"""SQLite storage for recent translations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dlx_tray.core.models import Language

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    file_name TEXT,
    provider TEXT NOT NULL
)
"""
_SELECT_ENTRIES = """
SELECT id, created_at, source_lang, target_lang, source_text, target_text, file_name, provider
FROM history
"""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: int
    created_at: datetime
    source_lang: Language
    target_lang: Language
    source_text: str
    target_text: str
    file_name: str | None
    provider: str


class HistoryStore:
    """Use a separate connection per operation so callers can work off the GUI thread."""

    def __init__(self, path: Path, limit: int) -> None:
        if limit < 1:
            raise ValueError("History limit must be positive")
        self._path = path
        # SQLite binds OFFSET as a signed 64-bit integer.
        self._prune_offset = min(limit - 1, 2**63 - 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, autocommit=False)

    def add(
        self,
        *,
        source_lang: Language,
        target_lang: Language,
        source_text: str,
        target_text: str,
        provider: str,
        file_name: str | None = None,
    ) -> None:
        """Save a translation and prune older entries in the same transaction."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO history (
                    created_at, source_lang, target_lang, source_text, target_text, file_name,
                    provider
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    source_lang.value,
                    target_lang.value,
                    source_text,
                    target_text,
                    file_name,
                    provider,
                ),
            )
            connection.execute(
                """
                DELETE FROM history WHERE id < (
                    SELECT id FROM history ORDER BY id DESC LIMIT 1 OFFSET ?
                )
                """,
                (self._prune_offset,),
            )

    def list(self) -> list[HistoryEntry]:
        """Return translations in reverse insertion order."""
        return self.search("")

    def search(self, query: str) -> list[HistoryEntry]:
        """Find literal substrings of the source or translation, ignoring Unicode case."""
        with closing(self._connect()) as connection:
            if query:
                connection.create_function("casefold", 1, str.casefold, deterministic=True)
                folded = query.casefold()
                escaped = folded.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                pattern = f"%{escaped}%"
                # LIKE stops at NUL and rejects patterns over the connection's byte limit.
                use_like = "\0" not in folded and len(
                    pattern.encode("utf-8")
                ) <= connection.getlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH)
                rows = connection.execute(
                    _SELECT_ENTRIES
                    + """
                    WHERE CASE WHEN :use_like AND instr(source_text, char(0)) = 0
                               THEN casefold(source_text) LIKE :pattern ESCAPE '!'
                               ELSE instr(casefold(source_text), :query) > 0 END
                       OR CASE WHEN :use_like AND instr(target_text, char(0)) = 0
                               THEN casefold(target_text) LIKE :pattern ESCAPE '!'
                               ELSE instr(casefold(target_text), :query) > 0 END
                    ORDER BY id DESC
                    """,
                    {"use_like": use_like, "pattern": pattern, "query": folded},
                )
            else:
                rows = connection.execute(_SELECT_ENTRIES + "ORDER BY id DESC")
            return [
                HistoryEntry(
                    id=entry_id,
                    created_at=datetime.fromisoformat(created_at),
                    source_lang=Language(source_lang),
                    target_lang=Language(target_lang),
                    source_text=source_text,
                    target_text=target_text,
                    file_name=file_name,
                    provider=provider,
                )
                for (
                    entry_id,
                    created_at,
                    source_lang,
                    target_lang,
                    source_text,
                    target_text,
                    file_name,
                    provider,
                ) in rows
            ]

    def delete(self, entry_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM history WHERE id = ?", (entry_id,))

    def clear(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM history")
