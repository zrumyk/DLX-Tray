"""Try the official API when the primary translation provider is unavailable."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from threading import Event, Lock
from time import monotonic

from dlx_tray.core.models import (
    Formality,
    Language,
    RateLimitedError,
    TranslationCancelledError,
    TranslationResult,
)
from dlx_tray.core.retry import RETRYABLE_ERRORS, RetryPolicy
from dlx_tray.providers.base import Translator

log = logging.getLogger(__name__)
_RETRY_DEFAULTS = RetryPolicy()


class FallbackTranslator:
    name = "fallback"

    def __init__(self, primary: Translator, secondary: Translator) -> None:
        self._primary = primary
        self._secondary = secondary
        self._lock = Lock()
        self._primary_blocked_until = 0.0

    def translate(
        self,
        texts: Sequence[str],
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
        *,
        cancel: Event | None = None,
    ) -> list[TranslationResult]:
        """Try the whole input on each provider in order, preserving result attribution."""
        if not texts:
            return []
        with self._lock:
            primary_available = monotonic() >= self._primary_blocked_until
        if primary_available:
            try:
                return self._primary.translate(texts, source, target, formality)
            except RETRYABLE_ERRORS as exc:
                if isinstance(exc, RateLimitedError):
                    delay = max(_RETRY_DEFAULTS.initial_delay, exc.retry_after or 0.0)
                    deadline = monotonic() + min(delay, _RETRY_DEFAULTS.max_retry_after)
                    with self._lock:
                        self._primary_blocked_until = max(self._primary_blocked_until, deadline)
                log.info(
                    "Translation provider %s failed (%s), falling back to %s",
                    self._primary.name,
                    type(exc).__name__,
                    self._secondary.name,
                )
        if cancel is not None and cancel.is_set():
            raise TranslationCancelledError("cancelled before fallback request")
        return self._secondary.translate(texts, source, target, formality)
