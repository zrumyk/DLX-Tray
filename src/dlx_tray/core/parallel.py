"""Schedule translation leaders in input order and yield results in that order."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from itertools import chain
from threading import Condition, Event
from time import monotonic

from dlx_tray.core.models import TranslationCancelledError, TranslationError, TranslationResult
from dlx_tray.core.retry import DispatchTicket, RequestDispatch
from dlx_tray.core.single_flight import SegmentFlights, SingleFlight

type Attempt = Callable[[RequestDispatch], TranslationResult]
type BatchAttempt[T] = Callable[[Sequence[T], RequestDispatch], Sequence[TranslationResult]]


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
    pair_with_next: bool = True


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
    *,
    can_pack: Callable[[Sequence[T], T], bool] = lambda _items, _next: False,
    batch: BatchAttempt[T] | None = None,
    batch_size: int = 1,
    wait_for_first_result: bool = False,
) -> Generator[tuple[T, TranslationResult | None]]:
    pending: deque[_Pending[T]] = deque()
    leaders: set[Future[TranslationResult]] = set()
    resolved_failures: dict[Future[TranslationResult], TranslationResult | None] = {}
    changed = Condition()
    stopped = _StopSignal(dispatch.cancel)
    job = object()
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

        def may_observe_cooldown(origin: DispatchTicket | None) -> bool:
            if origin is None or origin.job is not job or origin.index >= index:
                return True
            # The provider records a 429 before the worker publishes the attempt's outcome.
            # Let an earlier attempt finish before a speculative request uses its retry budget.
            with changed:
                return not any(
                    entry.index == origin.index
                    and entry.future is not None
                    and (not entry.future.done() or entry.failed())
                    for entry in pending
                )

        return replace(
            dispatch,
            cancel=stopped,
            may_start=may_start,
            on_start=on_start,
            ticket=DispatchTicket(job, index),
            may_observe_cooldown=may_observe_cooldown,
        )

    def claim(index: int, item: Work[T]) -> _Pending[T]:
        shared = item.shared
        future, leader = (
            shared.flights.claim(shared.key, shared.segment)
            if shared is not None
            else (Future(), True)
        )
        entry = _Pending(index, item, future, not leader)
        with changed:
            pending.append(entry)
        return entry

    def run_leaders(
        entries: list[_Pending[T]],
        request: RequestDispatch,
    ) -> None:
        first = entries[0].work
        try:
            if len(entries) > 1:
                assert batch is not None
                results = batch([entry.work.item for entry in entries], request)
            else:
                assert first.attempt is not None
                results = (first.attempt(request),)
            outcomes = tuple(zip(entries, results, strict=True))
        except BaseException as exc:
            # Resolve no member of a failed batch until every member can block later starts.
            with changed:
                for entry in entries:
                    future = entry.future
                    assert future is not None
                    shared = entry.work.shared
                    if shared is None:
                        future.set_exception(exc)
                    else:
                        shared.flights.fail(shared.key, future, exc)
        else:
            for entry, result in outcomes:
                assert entry.future is not None
                entry.future.set_result(result)
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
            if future in resolved_failures:
                assert shared is not None
                shared.flights.release(shared.key, shared.segment, future)
                recovered = resolved_failures[future]
                if recovered is None:
                    return retry()
                if on_shared is not None:
                    on_shared(recovered.provider)
                return recovered
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
        result = resolve(entry.work.item, get)
        if shared is not None and entry.failed():
            resolved_failures[future] = result
        return result

    inputs = enumerate(work)
    lookahead: tuple[int, Work[T]] | None = None
    # A file's first DLX response can start a retry or switch providers before more requests.
    first_request: Future[TranslationResult] | None = None
    exhausted = False
    with ThreadPoolExecutor(max_workers=dispatch.concurrency, thread_name_prefix="DLX") as pool:
        try:
            while pending or not exhausted:
                if dispatch.cancel.is_set():
                    stopped.set()
                    exhausted = True
                leaders = {future for future in leaders if not future.done()}
                with changed:
                    failed = any(entry.failed() for entry in pending)
                if (
                    not exhausted
                    and not failed
                    and len(leaders) < dispatch.concurrency
                    and started.is_set()
                    and (first_request is None or first_request.done())
                    and (dispatch.concurrency > 1 or not pending)
                ):
                    while (next_item := lookahead or next(inputs, None)) is not None:
                        index, item = next_item
                        lookahead = None
                        if item.attempt is None:
                            with changed:
                                pending.append(_Pending(index, item, None))
                            if len(pending) == 1 or dispatch.cancel.is_set():
                                break
                            continue
                        entry = claim(index, item)
                        future = entry.future
                        assert future is not None
                        if not entry.follower:
                            entries = [entry]
                            items = [item.item]
                            while (
                                len(entries) < batch_size
                                and entries[-1].work.pair_with_next
                                and not stopped.is_set()
                            ):
                                lookahead = next(inputs, None)
                                if (
                                    lookahead is None
                                    or lookahead[1].attempt is None
                                    or not can_pack(items, lookahead[1].item)
                                ):
                                    break
                                paired = claim(*lookahead)
                                lookahead = None
                                if paired.follower:
                                    break
                                entries.append(paired)
                                items.append(paired.work.item)
                            started = Event()
                            # The final result is published last and tracks this request's slot.
                            last_future = entries[-1].future
                            assert last_future is not None
                            if wait_for_first_result and first_request is None:
                                first_request = last_future
                            leaders.add(last_future)
                            pool.submit(
                                run_leaders,
                                entries,
                                dispatch_for(index, started),
                            )
                            break
                        if (
                            dispatch.concurrency == 1
                            or dispatch.cancel.is_set()
                            or entry.failed()
                            or (len(pending) == 1 and future.done())
                        ):
                            break
                    else:
                        exhausted = True
                if not pending:
                    continue
                head = pending[0]
                future = head.future
                with changed:
                    ready = future is None or future.done()
                if ready:
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
                        if future is not None and not future.done():
                            changed.wait(0.01)
            if stopped.is_set():
                # Retain untouched whitespace and file segments up to the next request.
                for _, item in chain((lookahead,) if lookahead is not None else (), inputs):
                    if item.attempt is not None:
                        break
                    yield item.item, None
                raise TranslationCancelledError("cancelled before all chunks were processed")
        finally:
            stopped.set()
