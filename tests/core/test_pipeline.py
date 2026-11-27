"""Exercise the translation job with a scripted translator and no network."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import TIMEOUT_MAX, Event
from typing import cast
from unittest.mock import Mock

import pytest

from dlx_tray.core import chunker
from dlx_tray.core.models import (
    AuthError,
    Chunk,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.pipeline import FailureAction, JobResult, TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.core.single_flight import SegmentFlights, SingleFlight
from dlx_tray.providers.fallback import FallbackTranslator
from dlx_tray.services.cache import TranslationCache, translation_key


class FakeTranslator:
    name = "fake"

    def __init__(self, *steps: Exception | Callable[[], None]) -> None:
        self.steps = iter(steps)
        self.calls: list[tuple[list[str], Language, Language, Formality]] = []

    def translate(
        self,
        texts: Sequence[str],
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
    ) -> list[TranslationResult]:
        self.calls.append((list(texts), source, target, formality))
        step = next(self.steps, None)
        if isinstance(step, Exception):
            raise step
        if step is not None:
            step()
        return [TranslationResult(text.upper(), Language.EN, self.name) for text in texts]


def test_streams_chunks_with_original_layout_without_a_default_pause() -> None:
    translator = FakeTranslator()
    waits: list[tuple[float, int]] = []
    progress: list[tuple[int, int, str]] = []
    job_cancel = Event()

    def sleep(seconds: float, cancel: Event) -> bool:
        assert cancel is job_cancel
        waits.append((seconds, len(translator.calls)))
        return True

    def on_progress(index: int, total: int, text: str) -> None:
        progress.append((index, total, text))

    job = TranslationJob(translator=translator, max_units=3, sleep=sleep)
    result = job.run(
        "\t one\r\n\r\ntwo  ",
        Language.AUTO,
        Language.UK,
        Formality.MORE,
        cancel=job_cancel,
        on_progress=on_progress,
    )

    assert translator.calls == [
        (["one"], Language.AUTO, Language.UK, Formality.MORE),
        (["two"], Language.AUTO, Language.UK, Formality.MORE),
    ]
    assert waits == []
    assert progress == [(1, 2, "\t ONE\r\n\r\n"), (2, 2, "\t ONE\r\n\r\nTWO  ")]
    assert result == JobResult("\t ONE\r\n\r\nTWO  ", Language.EN, chunk_providers=("fake", "fake"))


@pytest.mark.parametrize("text", ["", " \t\r\n "])
def test_non_translatable_input_is_returned_without_requests(text: str) -> None:
    translator = FakeTranslator()

    def on_progress(index: int, total: int, partial: str) -> None:
        pytest.fail("Non-translatable input must not report chunk progress")

    result = TranslationJob(translator=translator).run(
        text, Language.AUTO, Language.UK, on_progress=on_progress
    )

    assert result == JobResult(text, None)
    assert translator.calls == []


def test_cancel_before_request() -> None:
    translator = FakeTranslator()
    cancel = Event()
    cancel.set()

    result = TranslationJob(translator=translator).run(
        "one", Language.AUTO, Language.UK, cancel=cancel
    )

    assert result == JobResult("", None, cancelled=True)
    assert translator.calls == []


def test_cancel_during_inter_request_wait_keeps_completed_chunk() -> None:
    translator = FakeTranslator()

    def sleep(seconds: float, cancel: Event) -> bool:
        assert seconds == 2.5
        assert len(translator.calls) == 1
        cancel.set()
        return False

    job = TranslationJob(translator=translator, max_units=3, request_delay=2.5, sleep=sleep)
    result = job.run("one two", Language.AUTO, Language.UK)

    assert result == JobResult("ONE ", Language.EN, cancelled=True, chunk_providers=("fake",))
    assert len(translator.calls) == 1


def test_cancel_during_successful_request_keeps_its_translation() -> None:
    cancel = Event()
    translator = FakeTranslator(cancel.set)
    job = TranslationJob(translator=translator, max_units=3, request_delay=0)

    result = job.run("one two", Language.AUTO, Language.UK, cancel=cancel)

    assert result == JobResult("ONE ", Language.EN, cancelled=True, chunk_providers=("fake",))
    assert len(translator.calls) == 1


def test_cancel_during_backoff_does_not_ask_for_failure_action() -> None:
    translator = FakeTranslator(ProviderUnavailableError("offline"))

    def sleep(seconds: float, cancel: Event) -> bool:
        assert seconds == 2.0
        cancel.set()
        return False

    def on_failure(chunk: Chunk, error: TranslationError) -> FailureAction:
        pytest.fail("Cancellation must not ask for a failure action")

    job = TranslationJob(translator=translator, retry_policy=RetryPolicy(jitter=0), sleep=sleep)

    assert job.run("one", Language.AUTO, Language.UK, on_failure=on_failure) == JobResult(
        "", None, cancelled=True
    )
    assert len(translator.calls) == 1


@pytest.mark.parametrize(
    ("action", "expected", "sent", "progress_texts", "providers"),
    [
        (
            FailureAction.CONTINUE,
            "ONE TWO END",
            ["one", "two", "two", "end"],
            ["ONE ", "ONE TWO ", "ONE TWO END"],
            ("fake", "fake", "fake"),
        ),
        (
            FailureAction.SKIP,
            "ONE two END",
            ["one", "two", "end"],
            ["ONE ", "ONE two ", "ONE two END"],
            ("fake", None, "fake"),
        ),
        (FailureAction.CANCEL, "ONE ", ["one", "two"], ["ONE "], ("fake",)),
    ],
)
def test_failure_actions(
    action: FailureAction,
    expected: str,
    sent: list[str],
    progress_texts: list[str],
    providers: tuple[str | None, ...],
) -> None:
    error = ProviderUnavailableError("offline")
    translator = FakeTranslator(lambda: None, error)
    cancel = Event()
    failures: list[tuple[Chunk, TranslationError]] = []
    progress: list[tuple[int, int, str]] = []

    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        failures.append((chunk, exc))
        return action

    def on_progress(index: int, total: int, text: str) -> None:
        progress.append((index, total, text))

    job = TranslationJob(
        translator=translator,
        max_units=3,
        request_delay=0,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    result = job.run(
        "one two end",
        Language.AUTO,
        Language.UK,
        cancel=cancel,
        on_progress=on_progress,
        on_failure=on_failure,
    )

    assert [texts for texts, *_ in translator.calls] == [[text] for text in sent]
    assert failures == [(Chunk("two", " ", 1), error)]
    assert progress == [(index, 3, text) for index, text in enumerate(progress_texts, start=1)]
    assert cancel.is_set() is (action is FailureAction.CANCEL)
    assert result == JobResult(
        expected, Language.EN, cancelled=cancel.is_set(), chunk_providers=providers
    )


def test_unhandled_failure_returns_error_and_processed_prefix() -> None:
    error = AuthError("rejected")
    translator = FakeTranslator(lambda: None, error)
    job = TranslationJob(translator=translator, max_units=3, request_delay=0)

    result = job.run("one two end", Language.AUTO, Language.UK)

    assert result == JobResult("ONE ", Language.EN, error=error, chunk_providers=("fake",))
    assert len(translator.calls) == 2


@pytest.mark.parametrize("error_type", [AuthError, ProviderUnavailableError])
def test_request_that_fails_after_cancellation_returns_cancelled(
    error_type: type[TranslationError],
) -> None:
    cancel = Event()

    def fail() -> None:
        cancel.set()
        raise error_type("request failed")

    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        pytest.fail("Cancellation must take precedence over prompting for a failure action")

    def on_retry(attempt: int, delay: float, exc: TranslationError) -> None:
        pytest.fail("Cancellation must take precedence over scheduling a retry")

    translator = FakeTranslator(lambda: None, fail)
    job = TranslationJob(translator=translator, max_units=3, request_delay=0)
    result = job.run(
        "one two",
        Language.AUTO,
        Language.UK,
        cancel=cancel,
        on_failure=on_failure,
        on_retry=on_retry,
    )

    assert result == JobResult("ONE ", Language.EN, cancelled=True, chunk_providers=("fake",))
    assert len(translator.calls) == 2


def test_cancel_during_primary_failure_prevents_fallback() -> None:
    cancel = Event()

    def fail() -> None:
        cancel.set()
        raise ProviderUnavailableError("offline")

    primary = FakeTranslator(lambda: None, fail)
    secondary = FakeTranslator()
    job = TranslationJob(
        translator=FallbackTranslator(primary, secondary), max_units=3, request_delay=0
    )

    result = job.run("one two", Language.AUTO, Language.UK, cancel=cancel)

    assert result == JobResult("ONE ", Language.EN, cancelled=True, chunk_providers=("fake",))
    assert secondary.calls == []


def test_cancel_while_waiting_for_failure_action() -> None:
    cancel = Event()

    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        cancel.set()
        return FailureAction.SKIP

    job = TranslationJob(translator=FakeTranslator(AuthError("rejected")))
    result = job.run("one", Language.AUTO, Language.UK, cancel=cancel, on_failure=on_failure)

    assert result == JobResult("", None, cancelled=True)


def test_invalid_failure_action_is_rejected() -> None:
    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        return cast(FailureAction, "invalid")

    job = TranslationJob(translator=FakeTranslator(AuthError("rejected")))
    with pytest.raises(ValueError, match="invalid failure action"):
        job.run("one", Language.AUTO, Language.UK, on_failure=on_failure)


@pytest.mark.parametrize("delay", [-1.0, float("nan"), float("inf"), TIMEOUT_MAX * 2])
def test_invalid_delay_is_rejected(delay: float) -> None:
    with pytest.raises(ValueError, match="request_delay must be between"):
        TranslationJob(translator=FakeTranslator(), request_delay=delay)


def test_completed_paragraphs_survive_restart_without_splitting_or_pacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cache.db"
    translator = FakeTranslator()
    wait = Mock(return_value=True)
    job = TranslationJob(
        translator=translator,
        cache=TranslationCache(path),
        max_units=5,
        sleep=wait,
        request_delay=3.0,
    )
    text = "  first paragraph\r\n \r\nsecond  "
    assert job.run(text, Language.AUTO, Language.UK).text == text.upper()
    assert len(translator.calls) == 5
    assert wait.call_count == 4

    wait.reset_mock()
    translator.calls.clear()
    split = Mock(side_effect=AssertionError("a cached segment does not need chunking"))
    monkeypatch.setattr("dlx_tray.core.chunker.split_segment", split)
    progress = Mock()
    reopened = TranslationJob(
        translator=translator,
        cache=TranslationCache(path),
        max_units=100,
        sleep=wait,
        request_delay=3.0,
    )
    result = reopened.run(text, Language.AUTO, Language.UK, on_progress=progress)
    assert result == JobResult(text.upper(), Language.EN, chunk_providers=("fake", "fake"))
    progress.assert_called_with(2, 2, text.upper())
    assert not translator.calls
    split.assert_not_called()
    wait.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "source", "target", "formality", "text"),
    [
        ("deepl", Language.EN, Language.UK, Formality.DEFAULT, "entry"),
        ("fake", Language.AUTO, Language.UK, Formality.DEFAULT, "entry"),
        ("fake", Language.EN, Language.DE, Formality.DEFAULT, "entry"),
        ("fake", Language.EN, Language.UK, Formality.MORE, "entry"),
        ("fake", Language.EN, Language.UK, Formality.DEFAULT, "entry "),
    ],
)
def test_cache_misses_when_a_key_field_changes(
    tmp_path: Path,
    provider: str,
    source: Language,
    target: Language,
    formality: Formality,
    text: str,
) -> None:
    cache = TranslationCache(tmp_path / "cache.db")
    translator = FakeTranslator()
    job = TranslationJob(translator=translator, cache=cache)
    job.run("entry", Language.EN, Language.UK)
    translator.name = provider
    job.run(text, source, target, formality)
    assert len(translator.calls) == 2


@pytest.mark.parametrize("outcome", ["mixed", "failed", "skipped", "cancelled"])
def test_cache_keeps_incomplete_or_mixed_segments_out(tmp_path: Path, outcome: str) -> None:
    path = tmp_path / "cache.db"
    cache = TranslationCache(path)
    cancel = Event()
    primary = FakeTranslator(lambda: None, RateLimitedError("blocked"))
    secondary = FakeTranslator()
    secondary.name = "deepl"
    translator = FallbackTranslator(primary, secondary) if outcome == "mixed" else primary
    if outcome == "cancelled":
        primary = FakeTranslator(cancel.set)
        translator = primary
    job = TranslationJob(
        translator=translator, cache=cache, max_units=4, retry_policy=RetryPolicy(max_attempts=1)
    )
    result = job.run(
        "first uniq",
        Language.EN,
        Language.UK,
        cancel=cancel,
        on_failure=(lambda *_: FailureAction.SKIP) if outcome == "skipped" else None,
    )
    assert not path.exists()
    if outcome == "mixed":
        assert "deepl" in result.chunk_providers and "fake" in result.chunk_providers
    elif outcome == "failed":
        assert isinstance(result.error, RateLimitedError)
    elif outcome == "cancelled":
        assert result.cancelled
    else:
        assert None in result.chunk_providers


@pytest.mark.parametrize(
    "error", [RateLimitedError("blocked"), ProviderUnavailableError("unavailable")]
)
def test_fallback_segment_reopens_under_the_successful_provider(
    tmp_path: Path, error: TranslationError
) -> None:
    path = tmp_path / "cache.db"
    store = TranslationCache(path)
    primary = FakeTranslator(error)
    secondary = FakeTranslator()
    secondary.name = "deepl"
    job = TranslationJob(translator=FallbackTranslator(primary, secondary), cache=store)
    original = job.run("Sample", Language.AUTO, Language.UK)
    assert original.chunk_providers == ("deepl",)
    reopened = TranslationJob(
        translator=FallbackTranslator(primary, secondary), cache=TranslationCache(path)
    )
    assert reopened.run("Sample", Language.AUTO, Language.UK) == original
    assert len(primary.calls) == len(secondary.calls) == 1
    key = translation_key("fake", Language.AUTO, Language.UK, Formality.DEFAULT, "Sample")
    assert store.lookup(key, "fake") is None

    TranslationJob(translator=primary, cache=store).run("Sample", Language.AUTO, Language.UK)
    assert reopened.run("Sample", Language.AUTO, Language.UK).chunk_providers == ("fake",)
    assert len(primary.calls) == 2
    assert len(secondary.calls) == 1


@pytest.mark.parametrize("fails", [True, False])
def test_overlapping_jobs_share_distinct_chunks_and_release_a_failed_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    entered, follower_reserved, release = Event(), Event(), Event()
    flights = SingleFlight()
    reserve = flights.reserve
    reservations = 0

    def tracked_reserve(key: bytes, segment: SegmentFlights) -> Future[TranslationResult]:
        nonlocal reservations
        future = reserve(key, segment)
        reservations += 1
        if reservations == 4:
            follower_reserved.set()
        return future

    def first_request() -> None:
        entered.set()
        assert release.wait(5)
        if fails:
            raise ProviderUnavailableError("scripted failure")

    monkeypatch.setattr(flights, "reserve", tracked_reserve)
    translator = FakeTranslator(first_request)
    job = TranslationJob(
        translator=translator,
        cache=TranslationCache(tmp_path / "cache.db"),
        flights=flights,
        max_units=3,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(job.run, "one two", Language.EN, Language.UK)
        assert entered.wait(5)
        second = workers.submit(job.run, "one two", Language.EN, Language.UK)
        try:
            assert follower_reserved.wait(5)
        finally:
            release.set()
        results = first.result(5), second.result(5)
    if fails:
        assert all(isinstance(result.error, ProviderUnavailableError) for result in results)
        assert len(translator.calls) == 1
        assert job.run("one two", Language.EN, Language.UK).text == "ONE TWO"
        assert len(translator.calls) == 3
    else:
        assert [result.text for result in results] == ["ONE TWO", "ONE TWO"]
        assert len(translator.calls) == 2


@pytest.mark.parametrize("fails", [True, False])
def test_overlapping_segment_keeps_results_until_a_late_follower_finishes_planning(
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    request_started, planning, release_request, release_planning = (Event() for _ in range(4))
    split = chunker.split_segment

    def delay_second_plan(text: str, max_units: int) -> list[Chunk]:
        if request_started.is_set():
            planning.set()
            assert release_planning.wait(5)
        return split(text, max_units)

    def request() -> None:
        request_started.set()
        assert release_request.wait(5)
        if fails:
            raise ProviderUnavailableError("scripted failure")

    monkeypatch.setattr(chunker, "split_segment", delay_second_plan)
    translator = FakeTranslator(request)
    job = TranslationJob(
        translator=translator,
        flights=SingleFlight(),
        max_units=3,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        leader = workers.submit(job.run, "one two", Language.EN, Language.UK)
        assert request_started.wait(5)
        follower = workers.submit(job.run, "one two", Language.EN, Language.UK)
        try:
            assert planning.wait(5)
            release_request.set()
            first = leader.result(5)
        finally:
            release_request.set()
            release_planning.set()
        second = follower.result(5)
    if fails:
        assert isinstance(first.error, ProviderUnavailableError)
        assert isinstance(second.error, ProviderUnavailableError)
        assert len(translator.calls) == 1
        assert job.run("one two", Language.EN, Language.UK).text == "ONE TWO"
        assert len(translator.calls) == 3
    else:
        assert first.text == second.text == "ONE TWO"
        assert len(translator.calls) == 2


def test_follower_honors_pacing_before_dispatching_the_next_missing_chunk() -> None:
    leader_paused, release_leader = Event(), Event()

    def pause_leader(_processed: int, _total: int, text: str) -> None:
        if text == "ONE ":
            leader_paused.set()
            assert release_leader.wait(5)

    translator = FakeTranslator()
    wait = Mock(return_value=True)
    job = TranslationJob(
        translator=translator,
        flights=SingleFlight(),
        max_units=3,
        request_delay=2.5,
        sleep=wait,
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        leader = workers.submit(
            job.run, "one two", Language.EN, Language.UK, on_progress=pause_leader
        )
        try:
            assert leader_paused.wait(5)
            follower = workers.submit(job.run, "one two", Language.EN, Language.UK)
            assert follower.result(5).text == "ONE TWO"
        finally:
            release_leader.set()
        assert leader.result(5).text == "ONE TWO"
    assert [call[0] for call in translator.calls] == [["one"], ["two"]]
    assert wait.call_count == 1
    assert wait.call_args.args[0] == 2.5
