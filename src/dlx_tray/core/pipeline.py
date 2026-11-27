"""Translate chunks and publish the processed prefix in input order."""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import ExitStack
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
from dlx_tray.core.parallel import SharedAttempt, Work, ordered_attempts
from dlx_tray.core.retry import (
    DEFAULT_REQUEST_DELAY,
    RequestDispatch,
    RetryCallback,
    RetryPolicy,
    Sleeper,
    call_with_retry,
    event_sleep,
)
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import FallbackTranslator
from dlx_tray.services.cache import translation_key

if TYPE_CHECKING:
    from dlx_tray.core.single_flight import SingleFlight
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


@dataclass(slots=True)
class _Segment:
    source: Chunk
    scope: ExitStack = field(default_factory=ExitStack)
    first_part: int = 0
    first_provider: int = 0
    detected: Language | None = None


@dataclass(frozen=True, slots=True)
class _Piece:
    chunk: Chunk
    segment: _Segment
    first: bool
    last: bool
    cached: TranslationResult | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslationJob:
    translator: Translator
    max_units: int = chunker.DEFAULT_MAX_UNITS
    request_delay: float = DEFAULT_REQUEST_DELAY
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Sleeper = event_sleep
    cache: TranslationCache | None = None
    flights: SingleFlight | None = None
    concurrency: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.request_delay <= TIMEOUT_MAX:
            raise ValueError(f"request_delay must be between 0 and {TIMEOUT_MAX} seconds")
        if not chunker.MIN_MAX_UNITS <= self.max_units <= chunker.HARD_MAX_UNITS:
            raise ValueError("invalid maximum chunk size")
        if self.concurrency not in (1, 2):
            raise ValueError("concurrency must be 1 or 2")

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
        file_segments: Sequence[str] | None = None,
        on_segment: Callable[[str], None] | None = None,
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
        dlx = isinstance(translator, DeeplxTranslator) or (
            isinstance(translator, FallbackTranslator) and translator.has_dlx
        )
        concurrency = self.concurrency if dlx else 1
        dispatch = RequestDispatch(cancel, self.request_delay, concurrency, self.sleep)
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

        def request(chunk_text: str, attempt: RequestDispatch) -> TranslationResult:
            if isinstance(translator, FallbackTranslator):
                (result,) = translator.translate(
                    [chunk_text],
                    source,
                    target,
                    formality,
                    cancel=attempt.cancel,
                    on_provider=on_provider,
                    on_rate_limit=on_rate_limit,
                    dispatch=attempt if dlx else None,
                )
                return result
            if on_provider is not None:
                on_provider(translator.name)
            try:
                if isinstance(translator, DeeplxTranslator | DeepLOfficialTranslator):
                    (result,) = translator.translate(
                        [chunk_text], source, target, formality, dispatch=attempt
                    )
                else:
                    (result,) = translator.translate([chunk_text], source, target, formality)
            except RateLimitedError:
                if on_rate_limit is not None:
                    on_rate_limit(translator.name)
                raise
            return result

        def attempt(piece: _Piece, control: RequestDispatch) -> TranslationResult:
            return call_with_retry(
                lambda: request(piece.chunk.text, control),
                self.retry_policy,
                cancel=control.cancel,
                sleep=self.sleep,
                on_retry=on_retry,
            )

        def serial(
            work: Iterator[Work[_Piece]],
        ) -> Generator[tuple[_Piece, TranslationResult | None]]:
            for item in work:
                if item.attempt is None:
                    yield item.item, None
                    continue
                yield (
                    item.item,
                    self._serial_attempt(
                        item,
                        dispatch,
                        pacing,
                        cancel,
                        on_failure,
                        on_provider,
                        provider_pacing=dlx or isinstance(translator, DeepLOfficialTranslator),
                    ),
                )

        def report(processed: int) -> None:
            if on_progress is not None:
                on_progress(processed, total, "".join(parts))

        if file_segments is not None:
            segments = [Chunk(content, "", index) for index, content in enumerate(file_segments)]
        elif self.uses_segments and not file_segment:
            segments = chunker.paragraphs(text)
        else:
            segments = [Chunk(text, "", 0)]
        count_segments = self.uses_segments or file_segments is not None
        legacy_chunks = None if count_segments else chunker.split(text, self.max_units)
        total = (
            sum(bool(segment.text.strip()) for segment in segments)
            if legacy_chunks is None
            else sum(piece.translatable for piece in legacy_chunks)
        )
        processed = 0
        cache = self.cache
        flights = self.flights

        def plan(scopes: ExitStack) -> Iterator[Work[_Piece]]:
            for segment in segments:
                group = _Segment(segment)
                if not segment.text.strip():
                    blank = Chunk("", segment.text + segment.separator, segment.index)
                    yield Work(_Piece(blank, group, first=True, last=True))
                    continue
                shared = None
                if flights is not None:
                    scopes.callback(group.scope.close)
                    shared = group.scope.enter_context(
                        flights.segment(
                            translation_key(route, source, target, formality, segment.text)
                        )
                    )
                hit = (
                    self._cached_segment(
                        segment.text, source, target, formality, cache, provider_names
                    )
                    if cache is not None and not cancel.is_set()
                    else None
                )
                if hit is not None:
                    yield Work(_Piece(segment, group, first=True, last=True, cached=hit))
                    continue
                chunks = (
                    legacy_chunks
                    if legacy_chunks is not None
                    else chunker.split_segment(segment.text, self.max_units)
                )
                if segment.separator:
                    last = chunks[-1]
                    chunks[-1] = replace(last, separator=last.separator + segment.separator)
                keys = {}
                if flights is not None and shared is not None:
                    for chunk in chunks:
                        if chunk.translatable and chunk.text not in keys:
                            key = translation_key(route, source, target, formality, chunk.text)
                            flights.reserve(key, shared)
                            keys[chunk.text] = key
                for index, chunk in enumerate(chunks):
                    piece = _Piece(chunk, group, first=index == 0, last=index == len(chunks) - 1)
                    if not chunk.translatable:
                        yield Work(piece)
                        continue
                    reservation = (
                        SharedAttempt(flights, shared, keys[chunk.text])
                        if flights is not None and shared is not None
                        else None
                    )

                    def translate(
                        control: RequestDispatch, piece: _Piece = piece
                    ) -> TranslationResult:
                        return attempt(piece, control)

                    yield Work(piece, translate, reservation)

        def finish_segment(piece: _Piece) -> None:
            group = piece.segment
            if on_segment is not None or (cache is not None and not cancel.is_set()):
                translated = "".join(parts[group.first_part :])
                if group.source.separator:
                    translated = translated[: -len(group.source.separator)]
                if on_segment is not None:
                    on_segment(translated)
                if cache is not None and not cancel.is_set() and piece.cached is None:
                    names = set(providers[group.first_provider :])
                    if len(names) == 1:
                        (provider,) = names
                        if provider is not None:
                            key = translation_key(
                                provider, source, target, formality, group.source.text
                            )
                            cache.store(
                                key, TranslationResult(translated, group.detected, provider)
                            )
            group.scope.close()

        def resolve(
            piece: _Piece, translate: Callable[[], TranslationResult]
        ) -> TranslationResult | None:
            return self._translate_chunk(translate, piece.chunk, cancel, on_failure)

        failure: TranslationError | None = None
        cancelled = False
        try:
            with ExitStack() as scopes:
                tasks = plan(scopes)
                results = (
                    ordered_attempts(tasks, dispatch, resolve, on_provider)
                    if concurrency == 2
                    else serial(tasks)
                )
                scopes.callback(results.close)
                for piece, result in results:
                    chunk = piece.chunk
                    group = piece.segment
                    if piece.first:
                        group.first_part = len(parts)
                        group.first_provider = len(providers)
                    if chunk.translatable:
                        result = piece.cached or result
                        record(chunk, result)
                        if result is not None:
                            group.detected = group.detected or result.detected_source
                        if piece.cached is not None and on_provider is not None:
                            on_provider(piece.cached.provider)
                        if not count_segments or piece.last:
                            processed += 1
                        report(processed)
                    else:
                        parts.append(chunk.text + chunk.separator)
                    if piece.last:
                        finish_segment(piece)
        except TranslationCancelledError:
            cancelled = True
        except TranslationError as exc:
            failure = exc
        return JobResult("".join(parts), detected_source, cancelled, failure, tuple(providers))

    def _serial_attempt(
        self,
        item: Work[_Piece],
        dispatch: RequestDispatch,
        pacing: RequestPacing,
        cancel: Event,
        on_failure: FailureCallback | None,
        on_provider: Callable[[str], None] | None,
        *,
        provider_pacing: bool,
    ) -> TranslationResult | None:
        attempt = item.attempt
        assert attempt is not None
        started = False

        def run() -> TranslationResult:
            nonlocal started
            if not started and not provider_pacing:
                pacing.before_request(self.request_delay, self.sleep, cancel)
                started = True
            return attempt(dispatch)

        def translate() -> TranslationResult:
            nonlocal started
            shared = item.shared
            if shared is None:
                return run()
            try:
                result, leader = shared.flights.run(shared.key, shared.segment, run, cancel)
            except TranslationError:
                started = pacing.dispatched = True
                raise
            pacing.dispatched = True
            if not leader and on_provider is not None:
                on_provider(result.provider)
            return result

        return self._translate_chunk(translate, item.item.chunk, cancel, on_failure)

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
