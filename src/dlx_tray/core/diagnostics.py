"""Keep a bounded timeline of translation attempts and in-app waits."""

from collections import deque
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Literal

from dlx_tray.core.models import AnalysisTier
from dlx_tray.core.retry import Sleeper

type DiagnosticProvider = Literal["dlx", "deepl", "cloud_ai"]
type DLXConnection = Literal["starting", "ready", "unavailable", "external", "disabled"]
type WaitKind = Literal["pacing", "retry"]


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: DiagnosticProvider
    elapsed: float
    status: int | None
    retry_after: float | None = None
    cache: Literal["hit", "miss", "mixed"] | None = None
    analysis_tier: AnalysisTier | None = None


@dataclass(frozen=True, slots=True)
class TrayWait:
    seconds: float
    elapsed: float
    kind: WaitKind
    in_progress: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class _ActiveWait:
    seconds: float
    kind: WaitKind
    started: float


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
        self._active_waits: set[_ActiveWait] = set()
        self._cache_hits = 0
        self._cache_misses = 0
        self._retry_waits = 0
        self._retry_elapsed = 0.0
        self._lock = Lock()

    def append(self, event: ProviderAttempt | LocalCache | SharedResult) -> None:
        with self._lock:
            self._events.append(event)
            if isinstance(event, LocalCache):
                self._cache_hits += int(event.hit)
                self._cache_misses += int(not event.hit)

    def wait(
        self,
        seconds: float,
        cancel: Event,
        *,
        sleep: Sleeper,
        kind: WaitKind,
    ) -> bool:
        with self._lock:
            active = _ActiveWait(seconds, kind, monotonic())
            self._active_waits.add(active)
        try:
            return sleep(seconds, cancel)
        finally:
            with self._lock:
                elapsed = monotonic() - active.started
                self._active_waits.remove(active)
                self._events.append(TrayWait(seconds, elapsed, kind))
                if kind == "retry":
                    self._retry_waits += 1
                    self._retry_elapsed += elapsed

    def snapshot(self) -> DiagnosticSnapshot:
        with self._lock:
            now = monotonic()
            active = tuple(
                TrayWait(wait.seconds, now - wait.started, wait.kind, in_progress=True)
                for wait in sorted(self._active_waits, key=lambda item: item.started)
            )
            active_retries = [wait.elapsed for wait in active if wait.kind == "retry"]
            return DiagnosticSnapshot(
                (*self._events, *active),
                self._cache_hits,
                self._cache_misses,
                self._retry_waits + len(active_retries),
                self._retry_elapsed + sum(active_retries),
            )
