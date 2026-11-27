"""Exercise the translation job with a scripted translator and no network."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import TIMEOUT_MAX, Event
from typing import cast

import pytest

from dlx_tray.core.models import (
    AuthError,
    Chunk,
    Formality,
    Language,
    ProviderUnavailableError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.pipeline import FailureAction, JobResult, TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.providers.fallback import FallbackTranslator


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
