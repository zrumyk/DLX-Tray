"""Keep a bounded timeline of translation attempts and in-app waits."""

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Literal

type DiagnosticProvider = Literal["dlx", "deepl"]
type DLXConnection = Literal["starting", "ready", "unavailable", "external"]


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: DiagnosticProvider
    elapsed: float
    status: int | None
    retry_after: float | None = None
    cache: Literal["hit", "miss"] | None = None


@dataclass(frozen=True, slots=True)
class TrayWait:
    seconds: float


class DiagnosticTimeline:
    def __init__(self) -> None:
        self._events = deque[ProviderAttempt | TrayWait](maxlen=24)
        self._lock = Lock()

    def append(self, event: ProviderAttempt | TrayWait) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[ProviderAttempt | TrayWait, ...]:
        with self._lock:
            return tuple(self._events)
