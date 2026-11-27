"""Exercise the DLX client and retry policy over loopback HTTP."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING

import httpx
import pytest

from dlx_tray.core.chunker import HARD_MAX_UNITS, utf16_len
from dlx_tray.core.diagnostics import DiagnosticTimeline, LocalCache, SharedResult
from dlx_tray.core.models import (
    AuthError,
    Chunk,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.pipeline import FailureAction, JobResult, TranslationJob
from dlx_tray.core.retry import DispatchTicket, RequestDispatch, RetryPolicy, call_with_retry
from dlx_tray.core.single_flight import SingleFlight
from dlx_tray.providers import deeplx
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.services.cache import TranslationCache, translation_key
from tests.fakes.fake_dlx import FakeDLXFactory

if TYPE_CHECKING:
    from pathlib import Path

    from dlx_tray.core.single_flight import SegmentFlights


def test_parallel_cache_hits_and_followers_leave_room_for_distinct_misses(
    fake_dlx: FakeDLXFactory, tmp_path: Path
) -> None:
    first_started, last_started, release = Event(), Event(), Event()
    starts: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append(text)
        if text == "First":
            first_started.set()
            assert release.wait(5)
        elif text == "Last":
            last_started.set()
        return step

    diagnostics = DiagnosticTimeline()
    cache = TranslationCache(tmp_path / "cache.db")
    cache.store(
        translation_key("dlx", Language.EN, Language.UK, Formality.DEFAULT, "Cached"),
        TranslationResult("From cache", Language.EN, "dlx"),
    )
    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor() as executor,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            concurrency=2,
            cache=cache,
            flights=SingleFlight(),
            diagnostics=diagnostics,
        )
        result = executor.submit(
            job.run, "First\n\nCached\n\nFirst\n\nLast", Language.EN, Language.UK
        )
        try:
            assert first_started.wait(5)
            assert last_started.wait(5)
        finally:
            release.set()
        assert result.result(5).text == "[UK] First\n\nFrom cache\n\n[UK] First\n\n[UK] Last"
    assert sorted(starts) == ["First", "Last"]
    report = diagnostics.snapshot()
    assert (report.cache_hits, report.cache_misses) == (1, 3)
    assert report.events.count(LocalCache(hit=True)) == 1
    assert report.events.count(SharedResult()) == 1


def test_later_failure_stops_dispatch_and_preserves_only_the_prefix(
    fake_dlx: FakeDLXFactory,
) -> None:
    first_started, release_first, failed, release_failure = (Event() for _ in range(4))
    starts: list[str] = []
    progress: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append(text)
        if text == "First":
            first_started.set()
            assert release_first.wait(5)
        if text == "Second":
            failed.set()
            return "401"
        return step

    def on_failure(_chunk: Chunk, _error: TranslationError) -> FailureAction:
        assert release_failure.wait(5)
        return FailureAction.CANCEL

    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor() as executor,
    ):
        server.on_request = receive
        job = TranslationJob(translator=translator, concurrency=2, flights=SingleFlight())
        result = executor.submit(
            job.run,
            "First\n\nSecond\n\nThird",
            Language.EN,
            Language.UK,
            on_progress=lambda _i, _n, text: progress.append(text),
            on_failure=on_failure,
        )
        try:
            assert first_started.wait(5)
            assert failed.wait(5)
            assert progress == []
        finally:
            release_first.set()
            release_failure.set()
        outcome = result.result(5)
        assert outcome.cancelled
        assert outcome.text == "[UK] First\n\n"
    assert sorted(starts) == ["First", "Second"]


@pytest.mark.parametrize("cancel_job", [True, False], ids=["cancel", "cooldown"])
def test_parallel_dispatch_rechecks_state_after_pacing(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch, cancel_job: bool
) -> None:
    pacing, release_pacing, release_first, rated, cancel = (Event() for _ in range(5))
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    starts: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append(text)
        assert release_first.wait(5)
        return step if cancel_job else "429-retry-after"

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        pacing.set()
        assert release_pacing.wait(5)
        now += seconds
        return not cancel.is_set()

    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor() as executor,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            concurrency=2,
            flights=SingleFlight(),
            request_delay=0.5,
            sleep=sleep,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        result = executor.submit(
            job.run,
            "First\n\nSecond\n\nThird",
            Language.EN,
            Language.UK,
            cancel=cancel,
            on_rate_limit=lambda _provider: rated.set(),
        )
        try:
            assert pacing.wait(5)
            if cancel_job:
                cancel.set()
            release_first.set()
            if not cancel_job:
                assert rated.wait(5)
        finally:
            release_first.set()
            release_pacing.set()
        outcome = result.result(5)
        if cancel_job:
            assert outcome.cancelled and outcome.text == "[UK] First\n\n"
        else:
            assert isinstance(outcome.error, RateLimitedError)
            assert outcome.text == ""
    assert starts == ["First"]


def test_parallel_dlx_reopens_with_one_probe_after_a_429(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    old_started, release_old, rate_limited, probe_started, release_probe = (
        Event() for _ in range(5)
    )
    second_started, third_started, release_second = Event(), Event(), Event()
    starts: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append(text)
        if text == "Old":
            old_started.set()
            assert release_old.wait(5)
        elif text == "Limited":
            assert old_started.wait(5)
            return "429-retry-after"
        elif text == "Probe":
            probe_started.set()
            assert release_probe.wait(5)
        elif text == "Second":
            second_started.set()
            assert release_second.wait(5)
        elif text == "Third":
            third_started.set()
        return step

    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor() as executor,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            concurrency=2,
            flights=SingleFlight(),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        first = executor.submit(
            job.run,
            "Old\n\nLimited",
            Language.EN,
            Language.UK,
            on_rate_limit=lambda _provider: rate_limited.set(),
        )
        try:
            assert rate_limited.wait(5)
            now += 5
            with pytest.raises(RateLimitedError) as cooldown:
                translator.translate(["Too early"], Language.EN, Language.UK)
            assert cooldown.value.retry_after == 4
        finally:
            release_old.set()
        assert first.result(5).text == "[UK] Old\n\n"
        with pytest.raises(RateLimitedError):
            translator.translate(["Still too early"], Language.EN, Language.UK)
        now += 4

        recovered = executor.submit(job.run, "Probe\n\nSecond\n\nThird", Language.EN, Language.UK)
        try:
            assert probe_started.wait(5)
            assert not second_started.wait(0.1)
            release_probe.set()
            assert second_started.wait(5)
            assert third_started.wait(5)
        finally:
            release_probe.set()
            release_second.set()
        assert recovered.result(5).text == "[UK] Probe\n\n[UK] Second\n\n[UK] Third"
    assert sorted(starts) == ["Limited", "Old", "Probe", "Second", "Third"]


@pytest.mark.parametrize("action", [FailureAction.SKIP, FailureAction.CONTINUE])
def test_parallel_duplicate_reuses_the_resolved_attempt(
    fake_dlx: FakeDLXFactory, action: FailureAction
) -> None:
    late_started = Event()
    starts: list[str] = []
    failures: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append(text)
        if text == "Late":
            late_started.set()
        if text == "Twin" and starts.count("Twin") == 1:
            assert late_started.wait(5)
            return "401"
        return step

    def decide(chunk: Chunk, _error: TranslationError) -> FailureAction:
        failures.append(chunk.text)
        return action

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = receive
        result = TranslationJob(
            translator=translator, concurrency=2, max_units=4, flights=SingleFlight()
        ).run("Twin Twin Late", Language.EN, Language.UK, on_failure=decide)
    prefix = "Twin" if action is FailureAction.SKIP else "[UK] Twin"
    assert result.text == f"{prefix} [UK] Twin [UK] Late"
    assert failures == ["Twin"]
    assert sorted(starts) == ["Late", "Twin", "Twin"]


def test_parallel_skip_releases_a_paced_request_after_cooldown(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    pacing, deciding, release_pacing, decision_seen, cooldown_seen = (Event() for _ in range(5))
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    original_start = DeeplxTranslator._start

    def observe_start(translator: DeeplxTranslator, dispatch: RequestDispatch) -> int:
        if dispatch.ticket is not None and dispatch.ticket.index == 1:
            guard = dispatch.may_start
            observe = dispatch.may_observe_cooldown

            def observe_guard() -> bool:
                if deciding.is_set():
                    decision_seen.set()
                return guard()

            def observe_cooldown(origin: DispatchTicket | None) -> bool:
                observed = observe(origin)
                cooldown_seen.set()
                return observed

            dispatch = replace(
                dispatch, may_start=observe_guard, may_observe_cooldown=observe_cooldown
            )
        return original_start(translator, dispatch)

    monkeypatch.setattr(DeeplxTranslator, "_start", observe_start)

    def receive(texts: list[str], step: str) -> str:
        if texts == ["First"]:
            assert pacing.wait(5)
            return "429-retry-after"
        return step

    def wait_for_pacing(seconds: float, cancel: Event) -> bool:
        nonlocal now
        pacing.set()
        assert release_pacing.wait(5)
        now += seconds
        return not cancel.is_set()

    failures: list[str] = []

    def on_rate_limit(_provider: str) -> None:
        release_pacing.set()
        assert cooldown_seen.wait(5)

    def skip(chunk: Chunk, _error: TranslationError) -> FailureAction:
        nonlocal now
        failures.append(chunk.text)
        if chunk.text == "First":
            deciding.set()
            assert decision_seen.wait(5)
            now += 9
        return FailureAction.SKIP

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = receive
        result = TranslationJob(
            translator=translator,
            concurrency=2,
            flights=SingleFlight(),
            request_delay=0.5,
            sleep=wait_for_pacing,
            retry_policy=RetryPolicy(max_attempts=1),
        ).run(
            "First\n\nSecond",
            Language.EN,
            Language.UK,
            on_failure=skip,
            on_rate_limit=on_rate_limit,
        )
    assert failures == ["First"]
    assert result.text == "First\n\n[UK] Second"


def test_parallel_last_in_flight_success_completes_the_job(fake_dlx: FakeDLXFactory) -> None:
    cancel = Event()

    def receive(_texts: list[str], step: str) -> str:
        cancel.set()
        return step

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = receive
        result = TranslationJob(translator=translator, concurrency=2).run(
            "Finished", Language.EN, Language.UK, cancel=cancel
        )
    assert result == JobResult("[UK] Finished", Language.EN, chunk_providers=("dlx",))


@pytest.mark.parametrize(
    ("step", "error", "backoff_bounds"),
    [
        ("429", RateLimitedError, (15.0, 15.0)),
        ("429-retry-after", RateLimitedError, (9.0, 9.0)),
        ("429-retry-after-15", RateLimitedError, (15.0, 15.0)),
        ("503", ProviderUnavailableError, (1.6, 2.4)),
        ("slow", ProviderUnavailableError, (1.6, 2.4)),
        ("drop", ProviderUnavailableError, (1.6, 2.4)),
    ],
)
def test_job_retries_only_the_failed_chunk(
    fake_dlx: FakeDLXFactory,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    error: type[TranslationError],
    backoff_bounds: tuple[float, float],
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    waits: list[float] = []
    retries: list[tuple[int, float, type[TranslationError]]] = []
    progress: list[tuple[int, int, str]] = []
    first = "Hello" * 150 + "."
    second = "World" * 150 + "."

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += seconds
        waits.append(seconds)
        return not cancel.is_set()

    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        retries.append((attempt, seconds, type(exc)))

    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        pytest.fail("The transient error should recover before a failure decision is needed")

    def on_progress(index: int, total: int, text: str) -> None:
        progress.append((index, total, text))

    with (
        fake_dlx(["ok", step, "reverse"]) as server,
        DeeplxTranslator(
            server.url, server.token, timeout=0.5 if step == "slow" else 3
        ) as translator,
    ):
        job = TranslationJob(
            translator=translator,
            max_units=751,
            sleep=sleep,
        )
        result = job.run(
            f"{first}\r\n{second}",
            Language.AUTO,
            Language.UK,
            on_retry=on_retry,
            on_failure=on_failure,
            on_progress=on_progress,
        )

    assert len(waits) == 1
    assert backoff_bounds[0] <= waits[0] <= backoff_bounds[1]
    assert retries == [(1, waits[0], error)]
    prefix = f"[UK] {first}\r\n"
    expected = f"{prefix}[UK] {second[::-1]}"
    assert progress == [(1, 2, prefix), (2, 2, expected)]
    assert result == JobResult(expected, Language.EN, chunk_providers=("dlx", "dlx"))


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("units", [700, 750])
def test_batches_neighbouring_misses_and_keeps_the_tail(
    fake_dlx: FakeDLXFactory, concurrency: int, units: int, tmp_path: Path
) -> None:
    first = "\U0001f30d" * (units // 2)
    second = "B" * units
    tail = "Tail"
    source = f"{first}\r\n\r\n{second}\n\n{tail}  "
    requests: list[list[str]] = []
    progress: list[tuple[int, int, str]] = []
    cache = TranslationCache(tmp_path / "cache.db")

    def receive(texts: list[str], step: str) -> str:
        requests.append(texts)
        return step

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            concurrency=concurrency,
            max_units=units,
            cache=cache,
            flights=SingleFlight(),
        )
        result = job.run(
            source,
            Language.EN,
            Language.UK,
            on_progress=lambda *update: progress.append(update),
        )
        assert job.run(source, Language.EN, Language.UK) == result

    assert sorted(requests) == sorted([[first, second], [tail]])
    assert sum(utf16_len(text) for text in (first, second)) == units * 2
    prefix = f"[UK] {first}\r\n\r\n"
    middle = f"{prefix}[UK] {second}\n\n"
    expected = f"{middle}[UK] {tail}  "
    assert progress == [(1, 3, prefix), (2, 3, middle), (3, 3, expected)]
    assert result == JobResult(expected, Language.EN, chunk_providers=("dlx", "dlx", "dlx"))


@pytest.mark.parametrize("concurrency", [1, 2])
def test_batch_failure_resolves_each_chunk_individually(
    fake_dlx: FakeDLXFactory, concurrency: int
) -> None:
    requests: list[list[str]] = []
    failures: list[str] = []

    def receive(texts: list[str], step: str) -> str:
        requests.append(texts)
        return step

    def decide(chunk: Chunk, _error: TranslationError) -> FailureAction:
        failures.append(chunk.text)
        return FailureAction.SKIP if chunk.text == "Hello" else FailureAction.CONTINUE

    with (
        fake_dlx(["503", "reverse"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        server.on_request = receive
        result = TranslationJob(
            translator=translator,
            concurrency=concurrency,
            max_units=5,
            flights=SingleFlight(),
            retry_policy=RetryPolicy(max_attempts=1),
        ).run("Hello\r\nWorld", Language.EN, Language.UK, on_failure=decide)

    assert requests == [["Hello", "World"], ["World"]]
    assert failures == ["Hello", "World"]
    assert result == JobResult("Hello\r\n[UK] dlroW", Language.EN, chunk_providers=(None, "dlx"))


def test_neighbour_with_in_flight_batch_result_is_not_requested_again(
    fake_dlx: FakeDLXFactory,
) -> None:
    batch_started = Event()
    separate_started = Event()
    release_batch = Event()
    requests: list[list[str]] = []

    def receive(texts: list[str], step: str) -> str:
        requests.append(texts)
        if texts == ["Hello", "World"]:
            batch_started.set()
            assert release_batch.wait(5)
        elif texts == ["Other"]:
            separate_started.set()
        return step

    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor(2) as jobs,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator, max_units=5, concurrency=2, flights=SingleFlight()
        )
        batch = jobs.submit(job.run, "Hello World", Language.EN, Language.UK)
        try:
            assert batch_started.wait(5)
            overlap = jobs.submit(job.run, "Other World", Language.EN, Language.UK)
            assert separate_started.wait(5)
        finally:
            release_batch.set()
        assert batch.result(5).text == "[UK] Hello [UK] World"
        assert overlap.result(5).text == "[UK] Other [UK] World"

    assert requests == [["Hello", "World"], ["Other"]]


@pytest.mark.parametrize("shared", [False, True])
def test_serial_repeated_segment_hits_cache_before_pairing(
    fake_dlx: FakeDLXFactory, tmp_path: Path, shared: bool
) -> None:
    requests: list[list[str]] = []
    timeline = DiagnosticTimeline()

    def receive(texts: list[str], step: str) -> str:
        requests.append(texts)
        return step

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = receive
        result = TranslationJob(
            translator=translator,
            max_units=750,
            cache=TranslationCache(tmp_path / "cache.db"),
            flights=SingleFlight() if shared else None,
            diagnostics=timeline,
        ).run("Alpha\n\nAlpha\n\nBeta", Language.EN, Language.UK)

    assert requests == [["Alpha"], ["Beta"]]
    assert result.text == "[UK] Alpha\n\n[UK] Alpha\n\n[UK] Beta"
    assert timeline.snapshot().events == (LocalCache(False), LocalCache(True), LocalCache(False))


@pytest.mark.parametrize("concurrency", [1, 2])
def test_cancel_after_last_request_preserves_trailing_blank_file_segments(
    fake_dlx: FakeDLXFactory, concurrency: int
) -> None:
    completed: list[str] = []
    cancel = Event()
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        result = TranslationJob(
            translator=translator, max_units=750, concurrency=concurrency, flights=SingleFlight()
        ).run(
            "",
            Language.EN,
            Language.UK,
            cancel=cancel,
            file_segments=("Alpha", "  ", "\n"),
            on_segment=completed.append,
            on_progress=lambda *_update: cancel.set(),
        )

    assert completed == ["[UK] Alpha", "  ", "\n"]
    assert result == JobResult("[UK] Alpha  \n", Language.EN, chunk_providers=("dlx",))


def test_serial_batching_waits_for_an_earlier_pending_follower(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    retry_waiting, release_retry, follower_claimed, later_started = (Event() for _ in range(4))
    flights = SingleFlight()
    claim = flights.claim

    def observe_claim(
        key: bytes, segment: SegmentFlights
    ) -> tuple[Future[TranslationResult], bool]:
        future, leader = claim(key, segment)
        if not leader:
            follower_claimed.set()
        return future, leader

    def receive(texts: list[str], step: str) -> str:
        if texts == ["Alpha"]:
            return "503"
        later_started.set()
        return step

    def sleep(seconds: float, cancel: Event) -> bool:
        assert seconds > 0
        retry_waiting.set()
        assert release_retry.wait(5)
        return not cancel.is_set()

    monkeypatch.setattr(flights, "claim", observe_claim)
    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor(2) as jobs,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            max_units=750,
            flights=flights,
            retry_policy=RetryPolicy(max_attempts=2),
            sleep=sleep,
        )
        leader = jobs.submit(job.run, "Alpha", Language.EN, Language.UK)
        try:
            assert retry_waiting.wait(5)
            follower = jobs.submit(job.run, "Alpha\n\nBeta\n\nGamma", Language.EN, Language.UK)
            assert follower_claimed.wait(5)
            assert not later_started.wait(0.2)
        finally:
            release_retry.set()
        for future in (leader, follower):
            result = future.result(5)
            assert isinstance(result.error, ProviderUnavailableError) and result.text == ""
        assert not later_started.is_set()


@pytest.mark.parametrize("count_segments", [False, True])
def test_batch_streams_both_chunks_with_or_without_segment_progress(
    fake_dlx: FakeDLXFactory, count_segments: bool
) -> None:
    first, second = "A" * 700, "B" * 700
    updates: list[tuple[int, int, str]] = []
    with (
        fake_dlx(["ok", "403"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        result = TranslationJob(
            translator=translator, max_units=700, flights=SingleFlight() if count_segments else None
        ).run(
            f"{first}\r\n{second}",
            Language.EN,
            Language.UK,
            on_progress=lambda *update: updates.append(update),
        )
        assert server.next_step() == "403"

    prefix = f"[UK] {first}\r\n"
    expected = f"{prefix}[UK] {second}"
    counts = ((0, 1), (1, 1)) if count_segments else ((1, 2), (2, 2))
    assert updates == [(*counts[0], prefix), (*counts[1], expected)]
    assert result.text == expected


def test_batch_failure_is_ready_before_first_chunk_can_be_skipped(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    pacing, release_pacing, publishing_second, release_failure, skipped = (
        Event() for _ in range(5)
    )
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    flights = SingleFlight()
    fail = flights.fail
    beta_key = translation_key(repr(("dlx",)), Language.EN, Language.UK, Formality.DEFAULT, "Beta")
    requests: list[list[str]] = []

    def pause_second(key: bytes, future: Future[TranslationResult], error: BaseException) -> None:
        if key == beta_key:
            publishing_second.set()
            assert release_failure.wait(5)
        fail(key, future, error)

    def receive(texts: list[str], step: str) -> str:
        requests.append(texts)
        if texts == ["Alpha", "Beta"]:
            assert pacing.wait(5)
            return "401"
        return step

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        pacing.set()
        assert release_pacing.wait(5)
        now += seconds
        return not cancel.is_set()

    def decide(chunk: Chunk, _error: TranslationError) -> FailureAction:
        if chunk.text == "Alpha":
            skipped.set()
            return FailureAction.SKIP
        return FailureAction.CANCEL

    monkeypatch.setattr(flights, "fail", pause_second)
    with (
        fake_dlx([]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        ThreadPoolExecutor() as executor,
    ):
        server.on_request = receive
        job = TranslationJob(
            translator=translator,
            concurrency=2,
            max_units=750,
            flights=flights,
            request_delay=0.5,
            sleep=sleep,
        )
        result = executor.submit(
            job.run,
            "Alpha\n\nBeta\n\nGamma",
            Language.EN,
            Language.UK,
            on_failure=decide,
            on_progress=lambda *_update: release_pacing.set(),
        )
        try:
            assert publishing_second.wait(5)
            assert not skipped.wait(0.2)
        finally:
            release_failure.set()
            release_pacing.set()
        outcome = result.result(5)

    assert requests == [["Alpha", "Beta"]]
    assert outcome.cancelled and outcome.text == "Alpha\n\n"


@pytest.mark.parametrize(
    ("step", "error", "delay"),
    [
        ("429", RateLimitedError, 15.0),
        ("429-retry-after", RateLimitedError, 9.0),
        ("503", ProviderUnavailableError, 2.0),
        ("drop", ProviderUnavailableError, 2.0),
    ],
)
def test_retries_scripted_failure(
    fake_dlx: FakeDLXFactory,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    error: type[TranslationError],
    delay: float,
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    waits: list[float] = []
    failures: list[tuple[int, float, type[TranslationError]]] = []

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += seconds
        waits.append(seconds)
        return not cancel.is_set()

    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        failures.append((attempt, seconds, type(exc)))

    with fake_dlx([step, "ok"]) as server, DeeplxTranslator(server.url, server.token) as translator:
        result = call_with_retry(
            lambda: translator.translate(["Hello", "   ", "World"], Language.AUTO, Language.UK),
            RetryPolicy(max_attempts=2, jitter=0),
            sleep=sleep,
            on_retry=on_retry,
        )

    assert waits == [delay]
    assert failures == [(1, delay, error)]
    assert result == [
        TranslationResult("[UK] Hello", Language.EN, "dlx"),
        TranslationResult("   ", Language.EN, "dlx"),
        TranslationResult("[UK] World", Language.EN, "dlx"),
    ]


@pytest.mark.parametrize(
    ("step", "error"), [("401", AuthError), ("403", AuthError), ("413", TextTooLongError)]
)
def test_job_does_not_retry_scripted_client_errors(
    fake_dlx: FakeDLXFactory, step: str, error: type[TranslationError]
) -> None:
    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        pytest.fail(f"Unexpected retry after {exc}")

    with fake_dlx([step, "reverse"]) as server, DeeplxTranslator(server.url, server.token) as tr:
        result = TranslationJob(translator=tr).run(
            "Hello", Language.EN, Language.UK, on_retry=on_retry
        )
        assert isinstance(result.error, error)
        assert server.next_step() == "reverse"


def test_batch_at_limit_with_blank_entry(fake_dlx: FakeDLXFactory) -> None:
    text = "\U0001f600" * (HARD_MAX_UNITS // 4)
    blank = "\u2003" * (HARD_MAX_UNITS + 1)
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        result = translator.translate([text, blank, text], Language.EN, Language.UK)
    assert result == [
        TranslationResult(f"[UK] {text}", Language.EN, "dlx"),
        TranslationResult(blank, Language.EN, "dlx"),
        TranslationResult(f"[UK] {text}", Language.EN, "dlx"),
    ]


@pytest.mark.parametrize("file_mode", [False, True], ids=["editor", "file"])
def test_job_sends_exact_limit_and_splits_long_segments(
    fake_dlx: FakeDLXFactory, file_mode: bool
) -> None:
    exact = "\U0001f600" * (HARD_MAX_UNITS // 2)
    extra = "\U0001f642" * (HARD_MAX_UNITS // 2)
    text = f"{exact}\n\n{extra}x"
    requests: list[list[str]] = []
    translations: list[str] = []

    def record(texts: list[str], step: str) -> str:
        requests.append(texts)
        return step

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        server.on_request = record
        result = TranslationJob(translator=translator, flights=SingleFlight()).run(
            "" if file_mode else text,
            Language.EN,
            Language.UK,
            file_segments=[exact, f"{extra}x"] if file_mode else None,
            on_segment=translations.append if file_mode else None,
        )

    assert requests == [[exact], [extra], ["x"]]
    assert result.error is None
    rendered = "\n\n".join(translations) if file_mode else result.text
    assert rendered == f"[UK] {exact}\n\n[UK] {extra}[UK] x"


def test_cooldown_doubles_across_jobs_and_resets_on_success(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    with (
        fake_dlx(["429", "429", "ok", "429"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        job = TranslationJob(translator=translator, retry_policy=RetryPolicy(max_attempts=1))

        def run() -> JobResult:
            return job.run("Hello", Language.EN, Language.UK)

        def expect_cooldown(seconds: float) -> None:
            error = run().error
            assert isinstance(error, RateLimitedError)
            assert error.retry_after == seconds

        expect_cooldown(15.0)
        now += 5.0
        expect_cooldown(10.0)
        now += 10.0
        expect_cooldown(30.0)
        now += 30.0
        assert run().text == "[UK] Hello"
        expect_cooldown(15.0)


def test_slow_step_times_out_without_blocking_next_request(fake_dlx: FakeDLXFactory) -> None:
    with fake_dlx(["slow", "ok"]) as server:
        with DeeplxTranslator(server.url, server.token, timeout=0.5) as translator:
            with pytest.raises(ProviderUnavailableError, match="did not answer in time") as exc:
                translator.translate(["Hello"], Language.EN, Language.UK)
            assert isinstance(exc.value.__cause__, httpx.ReadTimeout)
        with DeeplxTranslator(server.url, server.token, timeout=3) as translator:
            assert translator.translate(["Hello"], Language.EN, Language.UK)[0].text == "[UK] Hello"


def test_health_auth_and_payload_validation_leave_script_intact(fake_dlx: FakeDLXFactory) -> None:
    with fake_dlx(["reverse"]) as server, httpx.Client(trust_env=False, timeout=3) as client:
        endpoint = f"{server.url}/translate"
        payload = {"text": "Hello", "target_lang": "UK"}
        assert client.get(server.url).json()["code"] == 200
        assert client.post(endpoint, json=payload).status_code == 401
        assert client.post(endpoint, params={"token": "wrong"}, json=payload).status_code == 401

        auth = {"Authorization": f"DeepL-Auth-Key {server.token}"}
        too_long = {**payload, "text": ["a", "\U0001f600" * 750]}
        assert client.post(endpoint, headers=auth, json=too_long).status_code == 413
        assert client.post(endpoint, headers=auth, json={**payload, "text": ""}).status_code == 404

        response = client.post(endpoint, params={"token": server.token}, json=payload)
        assert response.status_code == 200
        assert response.json() == {
            "code": 200,
            "data": "[UK] olleH",
            "source_lang": "EN",
            "target_lang": "UK",
            "alternatives": None,
            "id": 123,
            "method": "Free",
        }
        assert client.post(endpoint, headers=auth, json=payload).json()["data"] == "[UK] Hello"
