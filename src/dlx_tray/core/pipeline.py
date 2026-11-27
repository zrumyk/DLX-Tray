"""Run a cancellable, synchronous translation job one chunk at a time."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import TIMEOUT_MAX, Event
from typing import TYPE_CHECKING

from dlx_tray.core import chunker
from dlx_tray.core.models import (
    Chunk,
    Formality,
    Language,
    RateLimitedError,
    TranslationCancelledError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.retry import (
    DEFAULT_REQUEST_DELAY,
    RetryCallback,
    RetryPolicy,
    Sleeper,
    call_with_retry,
    event_sleep,
)
from dlx_tray.providers.fallback import FallbackTranslator
from dlx_tray.services.cache import translation_key

if TYPE_CHECKING:
    from dlx_tray.core.single_flight import SegmentFlights, SingleFlight
    from dlx_tray.providers.base import Translator
    from dlx_tray.services.cache import TranslationCache

log = logging.getLogger(__name__)


class FailureAction(StrEnum):
    CONTINUE = "continue"
    SKIP = "skip"
    CANCEL = "cancel"


type ProgressCallback = Callable[[int, int, str], None]
type FailureCallback = Callable[[Chunk, TranslationError], FailureAction]


@dataclass(frozen=True, slots=True)
class JobResult:
    """The processed prefix and the reason the job stopped, if it ended early."""

    text: str
    detected_source: Language | None
    cancelled: bool = False
    error: TranslationError | None = None
    # One entry per translated chunk or cached segment, with None for a skipped chunk.
    chunk_providers: tuple[str | None, ...] = ()


@dataclass(slots=True)
class RequestPacing:
    dispatched: bool = False

    def before_request(self, delay: float, sleep: Sleeper, cancel: Event) -> None:
        if self.dispatched and delay and not sleep(delay, cancel):
            raise TranslationCancelledError("cancelled during request pacing")
        self.dispatched = True


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslationJob:
    translator: Translator
    max_units: int = chunker.DEFAULT_MAX_UNITS
    request_delay: float = DEFAULT_REQUEST_DELAY
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Sleeper = event_sleep
    cache: TranslationCache | None = None
    flights: SingleFlight | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.request_delay <= TIMEOUT_MAX:
            raise ValueError(f"request_delay must be between 0 and {TIMEOUT_MAX} seconds")
        if not chunker.MIN_MAX_UNITS <= self.max_units <= chunker.HARD_MAX_UNITS:
            raise ValueError("invalid maximum chunk size")

    @property
    def uses_segments(self) -> bool:
        return self.cache is not None or self.flights is not None

    def run(
        self,
        text: str,
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
        *,
        cancel: Event | None = None,
        on_progress: ProgressCallback | None = None,
        on_failure: FailureCallback | None = None,
        on_retry: RetryCallback | None = None,
        on_provider: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str], None] | None = None,
        file_segment: bool = False,
        pacing: RequestPacing | None = None,
    ) -> JobResult:
        """Report progress and partial text after each translated or skipped chunk.

        Continue retries the failed chunk with a fresh retry budget. Skip keeps its source
        text. Without a failure callback, the result carries the error and processed prefix.
        An in-flight request can finish after cancellation and its result is kept.
        """
        if cancel is None:
            cancel = Event()
        if pacing is None:
            pacing = RequestPacing()
        translator = self.translator
        provider_names = (
            translator.provider_names()
            if isinstance(translator, FallbackTranslator)
            else (translator.name,)
        )
        route = repr(provider_names)
        parts: list[str] = []
        providers: list[str | None] = []
        detected_source: Language | None = None

        def record(chunk: Chunk, result: TranslationResult | None) -> None:
            nonlocal detected_source
            if result is None:
                parts.append(chunk.text + chunk.separator)
                providers.append(None)
            else:
                parts.append(result.text + chunk.separator)
                providers.append(result.provider)
                detected_source = detected_source or result.detected_source

        def request(chunk_text: str) -> TranslationResult:
            if isinstance(translator, FallbackTranslator):
                (result,) = translator.translate(
                    [chunk_text],
                    source,
                    target,
                    formality,
                    cancel=cancel,
                    on_provider=on_provider,
                    on_rate_limit=on_rate_limit,
                )
                return result
            if on_provider is not None:
                on_provider(translator.name)
            try:
                (result,) = translator.translate([chunk_text], source, target, formality)
            except RateLimitedError:
                if on_rate_limit is not None:
                    on_rate_limit(translator.name)
                raise
            return result

        def translate_chunk(
            chunk: Chunk, key: bytes | None, shared: SegmentFlights | None
        ) -> TranslationResult | None:
            started = False

            def retry_request() -> TranslationResult:
                nonlocal started
                if not started:
                    pacing.before_request(self.request_delay, self.sleep, cancel)
                    started = True
                return call_with_retry(
                    lambda: request(chunk.text),
                    self.retry_policy,
                    cancel=cancel,
                    sleep=self.sleep,
                    on_retry=on_retry,
                )

            def translate() -> TranslationResult:
                nonlocal started
                if flights is None or key is None or shared is None:
                    return retry_request()
                try:
                    result, leader = flights.run(key, shared, retry_request, cancel)
                except TranslationError:
                    # A shared failure still consumed an attempt for this chunk.
                    started = pacing.dispatched = True
                    raise
                pacing.dispatched = True
                if not leader and on_provider is not None:
                    on_provider(result.provider)
                return result

            return self._translate_chunk(translate, chunk, cancel, on_failure)

        def report(processed: int) -> None:
            if on_progress is not None:
                on_progress(processed, total, "".join(parts))

        segments = (
            chunker.paragraphs(text)
            if self.uses_segments and not file_segment
            else [Chunk(text, "", 0)]
        )
        legacy_chunks = None if self.uses_segments else chunker.split(text, self.max_units)
        total = (
            sum(piece.translatable for piece in legacy_chunks)
            if legacy_chunks is not None
            else sum(bool(segment.text.strip()) for segment in segments)
        )
        processed = 0
        cache = self.cache
        flights = self.flights

        def process_segment(segment: Chunk, shared: SegmentFlights | None) -> None:
            nonlocal processed
            if cache is not None and not cancel.is_set():
                hit = self._cached_segment(
                    segment.text, source, target, formality, cache, provider_names
                )
                if hit is not None:
                    record(segment, hit)
                    if on_provider is not None:
                        on_provider(hit.provider)
                    processed += 1
                    report(processed)
                    return

            chunks = (
                legacy_chunks
                if legacy_chunks is not None
                else chunker.split_segment(segment.text, self.max_units)
            )
            if segment.separator:
                last = chunks[-1]
                chunks[-1] = replace(last, separator=last.separator + segment.separator)
            keys = (
                {
                    chunk.text: translation_key(route, source, target, formality, chunk.text)
                    for chunk in chunks
                    if chunk.translatable
                }
                if flights is not None
                else {}
            )
            if flights is not None and shared is not None:
                for key in keys.values():
                    flights.reserve(key, shared)
            before = len(parts)
            first_provider = len(providers)
            segment_detected: Language | None = None
            for index, chunk in enumerate(chunks):
                if not chunk.translatable:
                    parts.append(chunk.separator)
                    continue
                result = translate_chunk(chunk, keys.get(chunk.text), shared)
                record(chunk, result)
                if result is not None:
                    segment_detected = segment_detected or result.detected_source
                if legacy_chunks is not None or index == len(chunks) - 1:
                    processed += 1
                report(processed)

            if cache is not None and not cancel.is_set():
                segment_providers = set(providers[first_provider:])
                if len(segment_providers) == 1:
                    (provider,) = segment_providers
                    if provider is not None:
                        translated = "".join(parts[before:])
                        if segment.separator:
                            translated = translated[: -len(segment.separator)]
                        key = translation_key(provider, source, target, formality, segment.text)
                        cache.store(key, TranslationResult(translated, segment_detected, provider))

        failure: TranslationError | None = None
        cancelled = False
        try:
            for segment in segments:
                if not segment.text.strip():
                    parts.append(segment.text + segment.separator)
                    continue
                scope = (
                    flights.segment(translation_key(route, source, target, formality, segment.text))
                    if flights is not None
                    else nullcontext()
                )
                with scope as shared:
                    process_segment(segment, shared)
        except TranslationCancelledError:
            cancelled = True
        except TranslationError as exc:
            failure = exc
        return JobResult("".join(parts), detected_source, cancelled, failure, tuple(providers))

    @staticmethod
    def _cached_segment(
        text: str,
        source: Language,
        target: Language,
        formality: Formality,
        cache: TranslationCache,
        provider_names: tuple[str, ...],
    ) -> TranslationResult | None:
        for provider in provider_names:
            key = translation_key(provider, source, target, formality, text)
            result = cache.lookup(key, provider)
            if result is not None:
                return result
        return None

    def _translate_chunk(
        self,
        translate: Callable[[], TranslationResult],
        chunk: Chunk,
        cancel: Event,
        on_failure: FailureCallback | None,
    ) -> TranslationResult | None:
        while True:
            try:
                return translate()
            except TranslationError as exc:
                log.warning("Translation failed for chunk %d (%s)", chunk.index, type(exc).__name__)
                if cancel.is_set():
                    raise TranslationCancelledError("cancelled during the request") from exc
                if on_failure is None:
                    raise
                action = on_failure(chunk, exc)
                if cancel.is_set() or action is FailureAction.CANCEL:
                    cancel.set()
                    raise TranslationCancelledError("cancelled on chunk failure") from exc
                if action is FailureAction.SKIP:
                    return None
                if action is not FailureAction.CONTINUE:
                    raise ValueError("invalid failure action") from exc
