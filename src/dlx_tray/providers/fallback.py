"""Try the official API when the primary translation provider is unavailable."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
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

    def replace_secondary(self, secondary: Translator) -> None:
        """Keep the primary cooldown when its fallback credentials change."""
        with self._lock:
            self._secondary = secondary

    def translate(
        self,
        texts: Sequence[str],
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
        *,
        cancel: Event | None = None,
        on_provider: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str], None] | None = None,
    ) -> list[TranslationResult]:
        """Try the whole input on each provider in order, preserving result attribution."""
        if not texts:
            return []
        with self._lock:
            primary_available = monotonic() >= self._primary_blocked_until
            secondary = self._secondary
        if primary_available:
            if on_provider is not None:
                on_provider(self._primary.name)
            try:
                return self._primary.translate(texts, source, target, formality)
            except RETRYABLE_ERRORS as exc:
                if isinstance(exc, RateLimitedError):
                    delay = max(_RETRY_DEFAULTS.initial_delay, exc.retry_after or 0.0)
                    deadline = monotonic() + delay
                    with self._lock:
                        self._primary_blocked_until = max(self._primary_blocked_until, deadline)
                    if on_rate_limit is not None:
                        on_rate_limit(self._primary.name)
                log.info(
                    "Translation provider %s failed (%s), falling back to %s",
                    self._primary.name,
                    type(exc).__name__,
                    secondary.name,
                )
        elif on_rate_limit is not None:
            on_rate_limit(self._primary.name)
        if cancel is not None and cancel.is_set():
            raise TranslationCancelledError("cancelled before fallback request")
        if on_provider is not None:
            on_provider(secondary.name)
        return secondary.translate(texts, source, target, formality)
