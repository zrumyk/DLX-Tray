"""Keep a bounded timeline of translation attempts and in-app waits."""

from collections import deque
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Literal

from dlx_tray.core.retry import Sleeper

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
    elapsed: float
    kind: Literal["pacing", "retry"]


@dataclass(frozen=True, slots=True)
class LocalCache:
    hit: bool


@dataclass(frozen=True, slots=True)
class SharedResult:
    """A single-flight follower received the leader's result."""


type DiagnosticEvent = ProviderAttempt | TrayWait | LocalCache | SharedResult


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    events: tuple[DiagnosticEvent, ...] = ()
    cache_hits: int = 0
    cache_misses: int = 0
    retry_waits: int = 0
    retry_elapsed: float = 0.0


class DiagnosticTimeline:
    def __init__(self) -> None:
        self._events = deque[DiagnosticEvent](maxlen=24)
        self._cache_hits = 0
        self._cache_misses = 0
        self._retry_waits = 0
        self._retry_elapsed = 0.0
        self._lock = Lock()

    def append(self, event: DiagnosticEvent) -> None:
        with self._lock:
            self._events.append(event)
            if isinstance(event, LocalCache):
                self._cache_hits += int(event.hit)
                self._cache_misses += int(not event.hit)
            elif isinstance(event, TrayWait) and event.kind == "retry":
                self._retry_waits += 1
                self._retry_elapsed += event.elapsed

    def wait(
        self,
        seconds: float,
        cancel: Event,
        *,
        sleep: Sleeper,
        kind: Literal["pacing", "retry"],
    ) -> bool:
        started = monotonic()
        try:
            return sleep(seconds, cancel)
        finally:
            self.append(TrayWait(seconds, monotonic() - started, kind))

    def snapshot(self) -> DiagnosticSnapshot:
        with self._lock:
            return DiagnosticSnapshot(
                tuple(self._events),
                self._cache_hits,
                self._cache_misses,
                self._retry_waits,
                self._retry_elapsed,
            )
