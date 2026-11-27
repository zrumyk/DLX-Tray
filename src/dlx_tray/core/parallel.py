"""Schedule translation leaders in input order and yield results in that order."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Condition, Event
from time import monotonic

from dlx_tray.core.models import TranslationCancelledError, TranslationError, TranslationResult
from dlx_tray.core.retry import RequestDispatch
from dlx_tray.core.single_flight import SegmentFlights, SingleFlight

type Attempt = Callable[[RequestDispatch], TranslationResult]


@dataclass(frozen=True, slots=True)
class SharedAttempt:
    flights: SingleFlight
    segment: SegmentFlights
    key: bytes


@dataclass(frozen=True, slots=True)
class Work[T]:
    item: T
    attempt: Attempt | None = None
    shared: SharedAttempt | None = None


@dataclass(frozen=True, slots=True)
class _Pending[T]:
    index: int
    work: Work[T]
    future: Future[TranslationResult] | None
    follower: bool = False

    def failed(self) -> bool:
        return (
            self.future is not None and self.future.done() and self.future.exception() is not None
        )


class _StopSignal(Event):
    def __init__(self, cancel: Event) -> None:
        super().__init__()
        self._cancel = cancel

    def is_set(self) -> bool:
        return self._cancel.is_set() or super().is_set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + timeout
        while not self.is_set():
            interval = 0.05 if deadline is None else min(0.05, deadline - monotonic())
            if interval <= 0:
                return False
            super().wait(interval)
        return True


def ordered_attempts[T](
    work: Iterator[Work[T]],
    dispatch: RequestDispatch,
    resolve: Callable[[T, Callable[[], TranslationResult]], TranslationResult | None],
    on_shared: Callable[[str], None] | None,
) -> Generator[tuple[T, TranslationResult | None]]:
    pending: deque[_Pending[T]] = deque()
    leaders: set[Future[TranslationResult]] = set()
    changed = Condition()
    stopped = _StopSignal(dispatch.cancel)
    started = Event()
    started.set()

    def dispatch_for(index: int, ready: Event) -> RequestDispatch:
        def may_start() -> bool:
            if stopped.is_set():
                raise TranslationCancelledError("cancelled before dispatch")
            with changed:
                return not any(entry.index < index and entry.failed() for entry in pending)

        def on_start() -> None:
            with changed:
                ready.set()
                changed.notify_all()

        return replace(dispatch, cancel=stopped, may_start=may_start, on_start=on_start)

    def run_leader(
        attempt: Attempt,
        shared: SharedAttempt | None,
        future: Future[TranslationResult],
        request: RequestDispatch,
    ) -> None:
        def run() -> TranslationResult:
            return attempt(request)

        try:
            if shared is not None:
                shared.flights.complete(shared.key, future, run)
            else:
                future.set_result(run())
        except BaseException as exc:
            if shared is None:
                future.set_exception(exc)
        finally:
            request.on_start()

    def result_for(entry: _Pending[T]) -> TranslationResult | None:
        future = entry.future
        attempt = entry.work.attempt
        assert future is not None and attempt is not None
        shared = entry.work.shared
        first = True

        def retry() -> TranslationResult:
            request = dispatch_for(entry.index, Event())
            if shared is None:
                return attempt(request)
            result, leader = shared.flights.run(
                shared.key, shared.segment, lambda: attempt(request), dispatch.cancel
            )
            if not leader and on_shared is not None:
                on_shared(result.provider)
            return result

        def get() -> TranslationResult:
            nonlocal first
            if not first:
                return retry()
            first = False
            try:
                result = future.result()
            except TranslationCancelledError:
                if not entry.follower or dispatch.cancel.is_set():
                    raise
                assert shared is not None
                shared.flights.release(shared.key, shared.segment, future)
                return retry()
            except TranslationError:
                if shared is not None:
                    shared.flights.release(shared.key, shared.segment, future)
                raise
            if entry.follower and on_shared is not None:
                on_shared(result.provider)
            return result

        # The caller resolves failures on its own thread, in input order.
        return resolve(entry.work.item, get)

    inputs = enumerate(work)
    exhausted = False
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="DLX") as pool:
        try:
            while pending or not exhausted:
                if dispatch.cancel.is_set():
                    stopped.set()
                    exhausted = True
                leaders = {future for future in leaders if not future.done()}
                with changed:
                    failed = any(entry.failed() for entry in pending)
                if not exhausted and not failed and len(leaders) < 2 and started.is_set():
                    for index, item in inputs:
                        if item.attempt is None:
                            with changed:
                                pending.append(_Pending(index, item, None))
                            break
                        shared = item.shared
                        future, leader = (
                            shared.flights.claim(shared.key, shared.segment)
                            if shared is not None
                            else (Future(), True)
                        )
                        with changed:
                            pending.append(_Pending(index, item, future, not leader))
                        if leader:
                            started = Event()
                            leaders.add(future)
                            pool.submit(
                                run_leader,
                                item.attempt,
                                shared,
                                future,
                                dispatch_for(index, started),
                            )
                            break
                        if future.done() or dispatch.cancel.is_set():
                            break
                    else:
                        exhausted = True
                if not pending:
                    continue
                head = pending[0]
                future = head.future
                if future is None or future.done():
                    result = None if future is None else result_for(head)
                    with changed:
                        pending.popleft()
                    yield head.work.item, result
                elif stopped.is_set() and head.follower:
                    raise TranslationCancelledError(
                        "cancelled while waiting for shared translation"
                    )
                else:
                    with changed:
                        if not future.done():
                            changed.wait(0.01)
            if stopped.is_set():
                raise TranslationCancelledError("cancelled before all chunks were processed")
        finally:
            stopped.set()
