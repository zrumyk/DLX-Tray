"""In-memory Firestore with serialized transactions and fault injection."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock

from google.api_core.exceptions import Aborted, AlreadyExists, NotFound, ServiceUnavailable


@dataclass
class Snapshot:
    id: str
    fields: dict[str, object] | None

    def to_dict(self) -> dict[str, object] | None:
        return deepcopy(self.fields)


@dataclass
class Document:
    db: FirestoreFake
    collection: str
    id: str

    def get(self, transaction: Transaction | None = None) -> Snapshot:
        if transaction is not None:
            assert transaction.active and not transaction.writes
        with self.db.lock:
            self.db.reads[self.collection, self.id] += 1
            if self.collection in self.db.read_errors:
                raise ServiceUnavailable("private upstream error")
            return Snapshot(self.id, deepcopy(self.db.data[self.collection].get(self.id)))

    def create(self, fields: dict[str, object]) -> None:
        with self.db.lock:
            if self.id in self.db.data[self.collection]:
                raise AlreadyExists("existing document")
            self.set(fields)

    def update(self, fields: dict[str, object]) -> None:
        with self.db.lock:
            if self.id not in self.db.data[self.collection]:
                raise NotFound("missing document")
            self.db.data[self.collection][self.id].update(deepcopy(fields))

    def set(self, fields: dict[str, object]) -> None:
        with self.db.lock:
            if self.collection in self.db.write_errors:
                raise ServiceUnavailable("private upstream error")
            self.db.data[self.collection][self.id] = deepcopy(fields)


@dataclass
class Collection:
    db: FirestoreFake
    name: str

    def document(self, key: str) -> Document:
        return Document(self.db, self.name, key)

    def stream(self) -> list[Snapshot]:
        with self.db.lock:
            return [
                Snapshot(key, deepcopy(fields)) for key, fields in self.db.data[self.name].items()
            ]


class Transaction:
    """Implements the Firestore SDK decorator's begin/commit/rollback lifecycle."""

    _max_attempts = 5
    _read_only = False

    def __init__(self, db: FirestoreFake) -> None:
        self.db = db
        self.active = False
        self.writes: list[tuple[str, Document, dict[str, object]]] = []
        self._id = b"test-transaction"

    def _clean_up(self) -> None:
        self._rollback()
        self.writes.clear()

    def _begin(self, retry_id: bytes | None) -> None:
        assert retry_id in (None, self._id)
        self.db.lock.acquire()
        self.active = True

    def _rollback(self) -> None:
        if self.active:
            self.active = False
            self.db.lock.release()

    def _commit(self) -> None:
        if self.db.aborts:
            self.db.aborts -= 1
            raise Aborted("retry transaction")
        before = deepcopy(self.db.data)
        try:
            for action, document, fields in self.writes:
                if action == "create":
                    document.create(fields)
                else:
                    document.update(fields)
        except (AlreadyExists, NotFound, ServiceUnavailable):
            self.db.data = before
            raise
        self._rollback()

    def create(self, document: Document, fields: dict[str, object]) -> None:
        self.writes.append(("create", document, fields))

    def update(self, document: Document, fields: dict[str, object]) -> None:
        self.writes.append(("update", document, fields))


class FirestoreFake:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, object]]] = {}
        self.lock = RLock()
        self.reads: Counter[tuple[str, str]] = Counter()
        self.read_errors: set[str] = set()
        self.write_errors: set[str] = set()
        self.aborts = 0
        self.closes = 0

    def collection(self, name: str) -> Collection:
        self.data.setdefault(name, {})
        return Collection(self, name)

    def transaction(self) -> Transaction:
        return Transaction(self)

    def close(self) -> None:
        self.closes += 1
