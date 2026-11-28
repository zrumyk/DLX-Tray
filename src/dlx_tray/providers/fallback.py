"""Translate with an ordered chain of compatible providers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from threading import Event, Lock
from time import monotonic

from dlx_tray.core import chunker
from dlx_tray.core.models import (
    DEFAULT_TRANSLATION_OPTIONS,
    CloudQuotaExceededError,
    ContextBlock,
    Formality,
    IndexedResult,
    IndexedSegment,
    Language,
    RateLimitedError,
    TranslationCancelledError,
    TranslationError,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.core.profiles import DEEPL_PROFILE, DLX_PROFILE, ProviderProfile
from dlx_tray.core.retry import (
    RETRYABLE_ERRORS,
    RequestDispatch,
    RequestStartClock,
    event_sleep,
    wait_for_request_start,
)
from dlx_tray.providers.base import BlockTranslator, Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator

log = logging.getLogger(__name__)
_MIN_COOLDOWN = 5.0
FALLBACK_ERRORS = (*RETRYABLE_ERRORS, CloudQuotaExceededError)
type Provider = Translator | BlockTranslator


class FallbackTranslator:
    name = "fallback"

    def __init__(
        self, primary: Provider, secondary: Provider, tertiary: Provider | None = None
    ) -> None:
        self._providers = (
            (primary, secondary) if tertiary is None else (primary, secondary, tertiary)
        )
        self._lock = Lock()
        self._blocked_until: dict[str, float] = {}
        self._quota_exhausted = False
        self._clock = (
            primary.start_clock
            if isinstance(primary, DeeplxTranslator)
            else RequestStartClock(monotonic)
        )

    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    @property
    def has_dlx(self) -> bool:
        return isinstance(self._providers[0], DeeplxTranslator)

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
        block = ContextBlock(tuple(IndexedSegment(index, text) for index, text in enumerate(texts)))
        return [
            item.result
            for item in self._translate(
                block,
                source,
                target,
                formality,
                DEFAULT_TRANSLATION_OPTIONS,
                dispatch or RequestDispatch(cancel or Event(), 0, 1, event_sleep),
                on_provider,
                on_rate_limit,
            )
        ]

    def _translate(
        self,
        block: ContextBlock,
        source: Language,
        target: Language,
        formality: Formality,
        options: TranslationOptions,
        dispatch: RequestDispatch,
        on_provider: Callable[[str], None] | None,
        on_rate_limit: Callable[[str], None] | None,
    ) -> Sequence[IndexedResult]:
        if not block.segments:
            return []
        for index, provider in enumerate(self._providers):
            if dispatch.cancel.is_set():
                raise TranslationCancelledError("cancelled before provider request")
            last = index == len(self._providers) - 1
            name = provider.name

            def started(name: str = name) -> None:
                if on_provider is not None:
                    on_provider(name)
                dispatch.on_start()

            control = replace(dispatch, clock=dispatch.clock or self._clock, on_start=started)
            with self._lock:
                quota_exhausted = name == "cloud_ai" and self._quota_exhausted
                cooldown = self._blocked_until.get(name, 0.0) - monotonic()
            try:
                if quota_exhausted:
                    raise CloudQuotaExceededError("Cloud AI character quota is exhausted")
                if cooldown > 0:
                    raise RateLimitedError("provider cooldown is active", cooldown)
                if isinstance(provider, BlockTranslator):
                    wait_for_request_start(control)
                    return provider.translate_block(block, source, target, options, control)
                return _translate_segments(provider, block, source, target, formality, control)
            except FALLBACK_ERRORS as exc:
                if isinstance(exc, CloudQuotaExceededError):
                    with self._lock:
                        self._quota_exhausted = True
                elif isinstance(exc, RateLimitedError) and not last:
                    if not isinstance(provider, DeeplxTranslator) and cooldown <= 0:
                        deadline = monotonic() + max(_MIN_COOLDOWN, exc.retry_after or 0.0)
                        with self._lock:
                            self._blocked_until[name] = max(
                                self._blocked_until.get(name, 0.0), deadline
                            )
                    if on_rate_limit is not None:
                        on_rate_limit(name)
                if last:
                    raise
                log.info(
                    "Translation provider %s failed (%s), falling back to %s",
                    name,
                    type(exc).__name__,
                    self._providers[index + 1].name,
                )
        raise AssertionError("fallback chain did not dispatch")


class BlockFallbackTranslator(FallbackTranslator):
    """Keep the primary provider's indexed context when dispatching a block."""

    def __init__(self, primary: BlockTranslator, secondary: Translator) -> None:
        super().__init__(primary, secondary)
        self.name = primary.name

    def translate_block(
        self,
        block: ContextBlock,
        source: Language,
        target: Language,
        options: TranslationOptions,
        dispatch: RequestDispatch,
        *,
        formality: Formality = Formality.DEFAULT,
        on_provider: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str], None] | None = None,
    ) -> Sequence[IndexedResult]:
        return self._translate(
            block, source, target, formality, options, dispatch, on_provider, on_rate_limit
        )


def _translate_segments(
    provider: Translator,
    block: ContextBlock,
    source: Language,
    target: Language,
    formality: Formality,
    dispatch: RequestDispatch,
) -> list[IndexedResult]:
    def send(texts: list[str]) -> list[TranslationResult]:
        if isinstance(provider, DeeplxTranslator):
            return provider.translate(
                texts,
                source,
                target,
                formality,
                dispatch=dispatch,
                minimum_cooldown=_MIN_COOLDOWN,
            )
        if isinstance(provider, DeepLOfficialTranslator):
            return provider.translate(texts, source, target, formality, dispatch=dispatch)
        wait_for_request_start(dispatch)
        return provider.translate(texts, source, target, formality)

    profile: ProviderProfile | None = {"dlx": DLX_PROFILE, "deepl": DEEPL_PROFILE}.get(
        provider.name
    )
    texts = [segment.text for segment in block.segments]
    if profile is None or (
        len(texts) <= profile.max_segments
        and sum(map(chunker.utf16_len, texts)) <= profile.max_units
    ):
        results = send(texts)
        if len(results) != len(texts):
            raise TranslationError("provider returned mismatched segment count")
        return [
            IndexedResult(segment.index, result)
            for segment, result in zip(block.segments, results, strict=True)
        ]

    # Keep each original segment's index and separators even when sent in pieces.
    output = []
    for segment in block.segments:
        chunks = chunker.split_segment(segment.text, profile.max_units)
        parts = []
        detected = None
        for chunk in chunks:
            translated = send([chunk.text]) if chunk.translatable else []
            if chunk.translatable:
                if len(translated) != 1:
                    raise TranslationError("provider returned mismatched segment count")
                parts.append(translated[0].text)
                detected = detected or translated[0].detected_source
            parts.append(chunk.separator)
        output.append(
            IndexedResult(segment.index, TranslationResult("".join(parts), detected, provider.name))
        )
    return output
