"""Share a pending translation attempt and its retry budget across callers."""

from __future__ import annotations

from concurrent.futures import Future, wait
from contextlib import contextmanager
from threading import Event, Lock
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from dlx_tray.core.models import TranslationCancelledError, TranslationError, TranslationResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class SegmentFlights:
    """The outcomes reserved for one caller processing an overlapping segment."""

    def __init__(self, peers: set[SegmentFlights]) -> None:
        self.peers = peers
        self.chunks: dict[bytes, Future[TranslationResult]] = {}


class SingleFlight:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: WeakValueDictionary[bytes, Future[TranslationResult]] = WeakValueDictionary()
        self._segments: dict[bytes, set[SegmentFlights]] = {}

    @contextmanager
    def segment(self, key: bytes) -> Iterator[SegmentFlights]:
        with self._lock:
            peers = self._segments.setdefault(key, set())
            segment = SegmentFlights(peers)
            for peer in peers:
                for chunk, future in peer.chunks.items():
                    if self._pending.get(chunk) is future:
                        segment.chunks[chunk] = future
            peers.add(segment)
        try:
            yield segment
        finally:
            with self._lock:
                peers.remove(segment)
                segment.chunks.clear()
                if not peers:
                    del self._segments[key]

    def reserve(self, key: bytes, segment: SegmentFlights) -> Future[TranslationResult]:
        with self._lock:
            reserved = segment.chunks.get(key)
            if reserved is not None:
                return reserved
            future = self._pending.get(key)
            if future is None:
                future = Future()
                self._pending[key] = future
            # A peer may still be planning. Reserve this outcome for it before dispatch.
            for peer in segment.peers:
                peer.chunks.setdefault(key, future)
            return future

    def run(
        self,
        key: bytes,
        segment: SegmentFlights,
        work: Callable[[], TranslationResult],
        cancel: Event,
    ) -> tuple[TranslationResult, bool]:
        while True:
            if cancel.is_set():
                raise TranslationCancelledError("cancelled before shared translation")
            future = self.reserve(key, segment)
            # Future.set_running_or_notify_cancel() expects one caller. Guard the claim.
            with self._lock:
                leader = not (future.running() or future.done())
                if leader:
                    future.set_running_or_notify_cancel()
            try:
                if leader:
                    return self._lead(key, future, work), True
                return self._await(future, cancel), False
            except TranslationCancelledError:
                if leader or cancel.is_set():
                    raise
                with self._lock:
                    del segment.chunks[key]
            except TranslationError:
                # A user's Continue gets a fresh attempt without hiding this failure
                # from the overlapping callers who reserved it.
                with self._lock:
                    del segment.chunks[key]
                raise

    def _lead(
        self, key: bytes, future: Future[TranslationResult], work: Callable[[], TranslationResult]
    ) -> TranslationResult:
        try:
            result = work()
        except BaseException as exc:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]
            future.set_exception(exc)
            raise
        future.set_result(result)
        return result

    def _await(
        self,
        future: Future[TranslationResult],
        cancel: Event,
    ) -> TranslationResult:
        while not wait((future,), timeout=0.05).done:
            if cancel.is_set():
                raise TranslationCancelledError("cancelled while waiting for shared translation")
        if cancel.is_set():
            raise TranslationCancelledError("cancelled while waiting for shared translation")
        return future.result()
