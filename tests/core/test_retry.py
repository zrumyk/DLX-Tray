"""Tests for the retry policy, driven by a fake clock."""

import random
import threading
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlx_tray.core.models import (
    AuthError,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationCancelledError,
    TranslationError,
)
from dlx_tray.core.retry import RetryPolicy, call_with_retry, event_sleep

NO_JITTER = RetryPolicy(jitter=0.0)


class FakeClock:
    """A sleeper that advances virtual time instantly and can cancel at a given moment."""

    def __init__(self, cancel_at: float | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.cancel_at = cancel_at

    def __call__(self, seconds: float, cancel: threading.Event) -> bool:
        self.sleeps.append(seconds)
        if self.cancel_at is not None and self.now + seconds >= self.cancel_at:
            self.now = max(self.now, self.cancel_at)
            cancel.set()
        else:
            self.now += seconds
        return not cancel.is_set()


class Scripted:
    """A callable that raises or returns the given outcomes in order and counts its calls."""

    def __init__(self, *outcomes: str | TranslationError) -> None:
        self.queue = list(outcomes)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        outcome = self.queue.pop(0)
        if isinstance(outcome, TranslationError):
            raise outcome
        return outcome


def test_success_first_time_does_not_sleep() -> None:
    clock = FakeClock()
    assert call_with_retry(Scripted("ok"), NO_JITTER, sleep=clock) == "ok"
    assert clock.sleeps == []


def test_backoff_doubles_from_five_seconds_and_caps_at_sixty() -> None:
    clock = FakeClock()
    errors = [ProviderUnavailableError("503")] * 6
    policy = RetryPolicy(max_attempts=7, jitter=0.0)
    assert call_with_retry(Scripted(*errors, "ok"), policy, sleep=clock) == "ok"
    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert clock.now == 195.0


def test_rate_limited_is_retried() -> None:
    clock = FakeClock()
    result = call_with_retry(Scripted(RateLimitedError("429"), "ok"), NO_JITTER, sleep=clock)
    assert result == "ok"
    assert clock.sleeps == [5.0]


@pytest.mark.parametrize(
    "error", [AuthError("401"), TextTooLongError("413"), TranslationError("other")]
)
def test_non_retryable_errors_propagate_immediately(error: TranslationError) -> None:
    clock = FakeClock()
    fn = Scripted(error, "ok")
    with pytest.raises(type(error)):
        call_with_retry(fn, NO_JITTER, sleep=clock)
    assert clock.sleeps == []
    assert fn.calls == 1


def test_non_translation_errors_propagate() -> None:
    def boom() -> str:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        call_with_retry(boom, NO_JITTER, sleep=FakeClock())


def test_gives_up_after_max_attempts_and_reraises_last_error() -> None:
    clock = FakeClock()
    last = ProviderUnavailableError("third")
    fn = Scripted(ProviderUnavailableError("1"), ProviderUnavailableError("2"), last, "ok")
    with pytest.raises(ProviderUnavailableError) as info:
        call_with_retry(fn, RetryPolicy(max_attempts=3, jitter=0.0), sleep=clock)
    assert info.value is last
    assert fn.calls == 3
    assert clock.sleeps == [5.0, 10.0]


def test_single_attempt_never_sleeps() -> None:
    clock = FakeClock()
    with pytest.raises(RateLimitedError):
        call_with_retry(Scripted(RateLimitedError("429")), RetryPolicy(max_attempts=1), sleep=clock)
    assert clock.sleeps == []


def test_retry_after_overrides_shorter_backoff() -> None:
    clock = FakeClock()
    fn = Scripted(RateLimitedError("429", retry_after=42.0), "ok")
    call_with_retry(fn, NO_JITTER, sleep=clock)
    assert clock.sleeps == [42.0]


def test_retry_after_shorter_than_backoff_keeps_backoff() -> None:
    clock = FakeClock()
    fn = Scripted(RateLimitedError("429", retry_after=1.0), "ok")
    call_with_retry(fn, NO_JITTER, sleep=clock)
    assert clock.sleeps == [5.0]


def test_retry_after_is_capped_and_negative_is_ignored() -> None:
    clock = FakeClock()
    fn = Scripted(
        RateLimitedError("429", retry_after=10_000.0),
        RateLimitedError("429", retry_after=-5.0),
        "ok",
    )
    call_with_retry(fn, NO_JITTER, sleep=clock)
    assert clock.sleeps == [300.0, 10.0]


def test_cancel_during_wait_raises_cancelled() -> None:
    clock = FakeClock(cancel_at=7.0)  # during the second wait (5 s, then 10 s)
    errors = [ProviderUnavailableError("503")] * 3
    fn = Scripted(*errors, "ok")
    with pytest.raises(TranslationCancelledError) as info:
        call_with_retry(fn, NO_JITTER, sleep=clock)
    assert isinstance(info.value.__cause__, ProviderUnavailableError)
    assert clock.now == 7.0
    assert fn.calls == 2


def test_cancel_before_first_attempt_makes_no_call() -> None:
    cancel = threading.Event()
    cancel.set()
    fn = Scripted("ok")
    with pytest.raises(TranslationCancelledError):
        call_with_retry(fn, NO_JITTER, cancel=cancel, sleep=FakeClock())
    assert fn.calls == 0


def test_cancelled_is_not_a_translation_error() -> None:
    assert not issubclass(TranslationCancelledError, TranslationError)


def test_on_retry_reports_each_wait() -> None:
    seen: list[tuple[int, float, str]] = []
    first, second = RateLimitedError("a"), ProviderUnavailableError("b")
    call_with_retry(
        Scripted(first, second, "ok"),
        NO_JITTER,
        sleep=FakeClock(),
        on_retry=lambda n, delay, err: seen.append((n, delay, str(err))),
    )
    assert seen == [(1, 5.0, "a"), (2, 10.0, "b")]


def test_jitter_varies_delay_within_bounds() -> None:
    policy = RetryPolicy()
    rng = random.Random(1234)
    delays = [policy.backoff(1, rng) for _ in range(200)]
    assert all(4.0 <= d <= 6.0 for d in delays)
    assert len(set(delays)) > 100


def test_seeded_rng_makes_jitter_reproducible() -> None:
    errors = [ProviderUnavailableError("503")] * 3
    first, second = FakeClock(), FakeClock()
    call_with_retry(Scripted(*errors, "ok"), sleep=first, rng=random.Random(7))
    call_with_retry(Scripted(*errors, "ok"), sleep=second, rng=random.Random(7))
    assert first.sleeps == second.sleeps
    assert first.sleeps != [5.0, 10.0, 20.0]


@given(retry_number=st.integers(min_value=1, max_value=10_000), seed=st.integers())
def test_backoff_never_exceeds_cap_or_goes_negative(retry_number: int, seed: int) -> None:
    delay = RetryPolicy().backoff(retry_number, random.Random(seed))
    assert 0.0 <= delay <= 60.0


def test_backoff_with_huge_multiplier_caps_instead_of_overflowing() -> None:
    policy = RetryPolicy(multiplier=1e10, jitter=0.0)
    assert policy.backoff(65, random.Random()) == 60.0


def test_backoff_rejects_retry_number_below_one() -> None:
    with pytest.raises(ValueError, match="retry_number"):
        RetryPolicy().backoff(0, random.Random())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"initial_delay": -1.0},
        {"max_delay": -1.0},
        {"max_retry_after": -1.0},
        {"multiplier": 0.5},
        {"jitter": 1.5},
        {"jitter": -0.1},
    ],
)
def test_policy_validates_its_fields(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_event_sleep_returns_true_when_not_cancelled() -> None:
    assert event_sleep(0.0, threading.Event()) is True


def test_event_sleep_wakes_up_on_cancel_from_another_thread() -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        assert event_sleep(30.0, cancel) is False
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5.0
