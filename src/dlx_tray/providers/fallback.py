"""Try the official API when the primary translation provider is unavailable."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from threading import Event, Lock
from time import monotonic

from dlx_tray.core.models import (
    Formality,
    Language,
    RateLimitedError,
    TranslationCancelledError,
    TranslationResult,
)
from dlx_tray.core.retry import RETRYABLE_ERRORS, RequestDispatch, event_sleep
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator

log = logging.getLogger(__name__)
_MIN_PRIMARY_COOLDOWN = 5.0


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

    def provider_names(self) -> tuple[str, str]:
        with self._lock:
            return self._primary.name, self._secondary.name

    @property
    def has_dlx(self) -> bool:
        return isinstance(self._primary, DeeplxTranslator)

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
        dispatch: RequestDispatch | None = None,
    ) -> list[TranslationResult]:
        """Try the whole input on each provider in order, preserving result attribution."""
        if not texts:
            return []
        primary = self._primary
        if isinstance(primary, DeeplxTranslator):
            dispatch = dispatch or RequestDispatch(cancel or Event(), 0, 1, event_sleep)
            dispatch = replace(dispatch, clock=primary.start_clock)

        def provider_dispatch(name: str, control: RequestDispatch) -> RequestDispatch:
            def started() -> None:
                if on_provider is not None:
                    on_provider(name)
                control.on_start()

            return replace(control, on_start=started)

        with self._lock:
            primary_available = monotonic() >= self._primary_blocked_until
            secondary = self._secondary
        if primary_available:
            try:
                if isinstance(primary, DeeplxTranslator):
                    assert dispatch is not None
                    return primary.translate(
                        texts,
                        source,
                        target,
                        formality,
                        dispatch=provider_dispatch(primary.name, dispatch),
                        minimum_cooldown=_MIN_PRIMARY_COOLDOWN,
                    )
                if on_provider is not None:
                    on_provider(primary.name)
                return primary.translate(texts, source, target, formality)
            except RETRYABLE_ERRORS as exc:
                if isinstance(exc, RateLimitedError):
                    if not self.has_dlx:
                        delay = max(_MIN_PRIMARY_COOLDOWN, exc.retry_after or 0.0)
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
        if isinstance(secondary, DeepLOfficialTranslator) and dispatch is not None:
            return secondary.translate(
                texts,
                source,
                target,
                formality,
                dispatch=provider_dispatch(secondary.name, dispatch),
            )
        if on_provider is not None:
            on_provider(secondary.name)
        return secondary.translate(texts, source, target, formality)
