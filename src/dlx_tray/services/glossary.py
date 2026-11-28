"""Store local glossaries and exchange them as UTF-8 CSV or TSV."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import StringIO
from itertools import islice
from pathlib import Path

from dlx_tray.contracts.glossary_match import MAX_BLOCK_TERMS, canonical_term, matching_terms
from dlx_tray.core.models import GlossaryEntry, Language
from dlx_tray.fileio.txt import write_txt

_COLUMNS = ("source_term", "target_term", "pos", "notes", "priority")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS glossaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    description TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary_entries (
    glossary_id INTEGER NOT NULL REFERENCES glossaries(id) ON DELETE CASCADE,
    term_key TEXT NOT NULL,
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    pos TEXT,
    notes TEXT,
    priority INTEGER NOT NULL,
    PRIMARY KEY (glossary_id, term_key)
);
"""


@dataclass(frozen=True, slots=True)
class Glossary:
    name: str
    source: Language
    target: Language
    description: str = ""
    id: int | None = None

    def supports(self, source: Language, target: Language) -> bool:
        return self.target == target and self.source in (Language.AUTO, source)


class GlossaryFormatError(ValueError):
    """A glossary contains invalid metadata or entries."""


def validate_entries(entries: Iterable[GlossaryEntry]) -> tuple[GlossaryEntry, ...]:
    result = {}
    for entry in entries:
        source, target = entry.source_term.strip(), entry.target_term.strip()
        fields = (source, target, entry.pos or "", entry.notes or "")
        if not source or not target or any("\0" in field for field in fields):
            raise GlossaryFormatError("Invalid glossary term")
        key = canonical_term(source)
        if key in result:
            raise GlossaryFormatError("Duplicate glossary term")
        if not -(2**63) <= entry.priority < 2**63:
            raise GlossaryFormatError("Glossary priority is out of range")
        result[key] = replace(entry, source_term=source, target_term=target)
    return tuple(result.values())


class GlossaryMatcher:
    """Resolve document overrides before ranking the matches for each request."""

    def __init__(
        self, saved: Iterable[GlossaryEntry], document: Iterable[GlossaryEntry] = ()
    ) -> None:
        winners: dict[str, tuple[int, GlossaryEntry]] = {}
        for layer, entries in enumerate((document, saved)):
            for entry in entries:
                key = canonical_term(entry.source_term)
                old = winners.get(key)
                rank = (layer, -entry.priority, entry.target_term)
                if key and (old is None or rank < (old[0], -old[1].priority, old[1].target_term)):
                    winners[key] = (layer, replace(entry, source_term=key))
        self._entries = {
            entry.source_term: entry
            for _, entry in sorted(
                winners.values(), key=lambda pair: (pair[0], -pair[1].priority, pair[1].source_term)
            )
        }

    def for_block(self, segments: Iterable[str]) -> tuple[GlossaryEntry, ...]:
        sources = matching_terms(segments, self._entries)
        return tuple(self._entries[term] for term in islice(sources, MAX_BLOCK_TERMS))


def _delimiter(path: Path) -> str:
    match path.suffix.lower():
        case ".csv":
            return ","
        case ".tsv":
            return "\t"
        case _:
            raise GlossaryFormatError("Expected a CSV or TSV file")


class GlossaryStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection

    def list(self) -> tuple[Glossary, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, source_lang, target_lang, description, id FROM glossaries "
                "ORDER BY name COLLATE NOCASE, id"
            )
            return tuple(
                Glossary(name, Language(source), Language(target), description, glossary_id)
                for name, source, target, description, glossary_id in rows
            )

    @staticmethod
    def _read_entries(
        connection: sqlite3.Connection, glossary_id: int
    ) -> tuple[GlossaryEntry, ...]:
        if (
            connection.execute("SELECT 1 FROM glossaries WHERE id = ?", (glossary_id,)).fetchone()
            is None
        ):
            raise GlossaryFormatError("Glossary no longer exists")
        rows = connection.execute(
            "SELECT source_term, target_term, priority, pos, notes FROM glossary_entries "
            "WHERE glossary_id = ? ORDER BY term_key",
            (glossary_id,),
        )
        return tuple(GlossaryEntry(*row) for row in rows)

    def entries(self, glossary_id: int) -> tuple[GlossaryEntry, ...]:
        with self._connect() as connection:
            return self._read_entries(connection, glossary_id)

    @staticmethod
    def _insert_entries(
        connection: sqlite3.Connection, glossary_id: int, entries: Iterable[GlossaryEntry]
    ) -> None:
        connection.executemany(
            "INSERT INTO glossary_entries "
            "(glossary_id, term_key, source_term, target_term, pos, notes, priority) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (glossary_id, term_key) DO UPDATE SET "
            "source_term=excluded.source_term, target_term=excluded.target_term, "
            "pos=excluded.pos, notes=excluded.notes, priority=excluded.priority",
            (
                (
                    glossary_id,
                    canonical_term(e.source_term),
                    e.source_term,
                    e.target_term,
                    e.pos,
                    e.notes,
                    e.priority,
                )
                for e in entries
            ),
        )

    def save(self, glossary: Glossary, entries: Iterable[GlossaryEntry]) -> Glossary:
        """Create a glossary or replace its metadata and entries in one transaction."""
        name = glossary.name.strip()
        if not name or "\0" in name or "\0" in glossary.description:
            raise GlossaryFormatError("Invalid glossary metadata")
        if not glossary.source.can_be_source or not glossary.target.can_be_target:
            raise GlossaryFormatError("Invalid glossary languages")
        entries = validate_entries(entries)
        now = datetime.now(UTC).isoformat()
        values = (name, glossary.source.value, glossary.target.value, glossary.description, now)
        with self._connect(write=True) as connection:
            glossary_id = glossary.id
            if glossary_id is None:
                cursor = connection.execute(
                    "INSERT INTO glossaries "
                    "(name, source_lang, target_lang, description, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    values,
                )
                glossary_id = cursor.lastrowid
                assert glossary_id is not None
            else:
                cursor = connection.execute(
                    "UPDATE glossaries SET name=?, source_lang=?, target_lang=?, description=?, "
                    "updated_at=? WHERE id=?",
                    (*values, glossary_id),
                )
                if cursor.rowcount != 1:
                    raise GlossaryFormatError("Glossary no longer exists")
                connection.execute(
                    "DELETE FROM glossary_entries WHERE glossary_id=?", (glossary_id,)
                )
            self._insert_entries(connection, glossary_id, entries)
        return replace(glossary, id=glossary_id, name=name)

    def merge(self, glossary_id: int, entries: Iterable[GlossaryEntry]) -> None:
        entries = validate_entries(entries)
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE glossaries SET updated_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), glossary_id),
            )
            if cursor.rowcount != 1:
                raise GlossaryFormatError("Glossary no longer exists")
            self._insert_entries(connection, glossary_id, entries)

    def delete(self, glossary_id: int) -> None:
        with self._connect(write=True) as connection:
            connection.execute("DELETE FROM glossaries WHERE id=?", (glossary_id,))

    def import_file(self, path: Path, glossary: Glossary) -> Glossary:
        """Read and validate every row before changing the database."""
        delimiter = _delimiter(path)
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = csv.reader(stream, delimiter=delimiter, strict=True)
                header = next(rows, [])
                if (
                    not set(_COLUMNS[:2]).issubset(header)
                    or len(set(header)) != len(header)
                    or not set(header).issubset(_COLUMNS)
                ):
                    raise GlossaryFormatError("Invalid glossary header")
                entries = []
                for row in rows:
                    if not row:
                        continue
                    if len(row) != len(header):
                        raise GlossaryFormatError("Invalid glossary row length")
                    fields = dict(zip(header, row, strict=True))
                    entries.append(
                        GlossaryEntry(
                            fields["source_term"],
                            fields["target_term"],
                            int(fields.get("priority") or "0"),
                            fields.get("pos") or None,
                            fields.get("notes") or None,
                        )
                    )
        except (csv.Error, ValueError) as exc:
            raise GlossaryFormatError("Invalid CSV or TSV glossary") from exc
        return self.save(glossary, entries)

    def export_file(self, glossary_id: int, path: Path) -> None:
        output = StringIO(newline="")
        writer = csv.writer(output, delimiter=_delimiter(path))
        writer.writerow(_COLUMNS)
        writer.writerows(
            (e.source_term, e.target_term, e.pos, e.notes, e.priority)
            for e in self.entries(glossary_id)
        )
        write_txt(path, output.getvalue())
