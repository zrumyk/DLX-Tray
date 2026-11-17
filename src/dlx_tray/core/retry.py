"""Retry policy: exponential backoff with jitter for transient provider failures."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from dlx_tray.core.models import (
    ProviderUnavailableError,
    RateLimitedError,
    TranslationCancelledError,
    TranslationError,
)

RETRYABLE_ERRORS: tuple[type[TranslationError], ...] = (RateLimitedError, ProviderUnavailableError)
"""Rate limiting (429) and an unreachable or failing upstream (connection error, 503)."""


class Sleeper(Protocol):
    def __call__(self, seconds: float, cancel: threading.Event) -> bool:
        """Wait ``seconds``; return False as soon as ``cancel`` is set, True otherwise."""
        ...


def event_sleep(seconds: float, cancel: threading.Event) -> bool:
    """The real sleeper: an interruptible wait on the cancel event."""
    return not cancel.wait(seconds)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often and how long to wait between attempts.

    The n-th retry waits ``initial_delay * multiplier ** (n - 1)`` seconds, capped at
    ``max_delay``, scaled by a random factor in ``[1 - jitter, 1 + jitter]`` and capped again.
    A server-sent ``Retry-After`` raises the wait to that value, up to ``max_retry_after``.
    """

    max_attempts: int = 5
    initial_delay: float = 5.0
    multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.2
    max_retry_after: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.initial_delay < 0 or self.max_delay < 0 or self.max_retry_after < 0:
            raise ValueError("delays must be non-negative")
        if self.multiplier < 1:
            raise ValueError(f"multiplier must be >= 1, got {self.multiplier}")
        if not 0 <= self.jitter <= 1:
            raise ValueError(f"jitter must be in [0, 1], got {self.jitter}")

    def backoff(self, retry_number: int, rng: random.Random) -> float:
        """The jittered wait before retry ``retry_number`` (1-based)."""
        if retry_number < 1:
            raise ValueError(f"retry_number must be >= 1, got {retry_number}")
        # Cap the exponent too, so a huge retry_number can't overflow the float.
        exponent = min(retry_number - 1, 64)
        base = min(self.initial_delay * self.multiplier**exponent, self.max_delay)
        factor = rng.uniform(1 - self.jitter, 1 + self.jitter)
        return min(base * factor, self.max_delay)

    def delay_for(self, error: TranslationError, retry_number: int, rng: random.Random) -> float:
        """The wait before retry ``retry_number`` after ``error``, honouring Retry-After."""
        delay = self.backoff(retry_number, rng)
        if isinstance(error, RateLimitedError) and error.retry_after is not None:
            delay = max(delay, min(max(error.retry_after, 0.0), self.max_retry_after))
        return delay


RetryCallback = Callable[[int, float, TranslationError], None]
"""Called before each wait with (retry_number, delay_seconds, error)."""


def call_with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    cancel: threading.Event | None = None,
    sleep: Sleeper = event_sleep,
    rng: random.Random | None = None,
    on_retry: RetryCallback | None = None,
) -> T:
    """Call ``fn`` until it succeeds, fails with a non-retryable error, or attempts run out.

    The last error is re-raised when attempts run out. ``TranslationCancelledError`` is raised
    if ``cancel`` is set before an attempt or during a wait.
    """
    policy = policy or RetryPolicy()
    cancel = cancel or threading.Event()
    rng = rng or random.Random()
    attempt = 1
    while True:
        if cancel.is_set():
            raise TranslationCancelledError("cancelled before the request")
        try:
            return fn()
        except RETRYABLE_ERRORS as error:
            if attempt >= policy.max_attempts:
                raise
            delay = policy.delay_for(error, attempt, rng)
            if on_retry is not None:
                on_retry(attempt, delay, error)
            if not sleep(delay, cancel):
                raise TranslationCancelledError("cancelled while waiting to retry") from error
            attempt += 1
