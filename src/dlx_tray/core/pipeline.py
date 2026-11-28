"""Translate chunks and publish the processed prefix in input order."""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from itertools import count
from threading import TIMEOUT_MAX, Event
from time import monotonic
from typing import TYPE_CHECKING

from dlx_tray.core import chunker
from dlx_tray.core.diagnostics import DiagnosticTimeline, LocalCache, SharedResult
from dlx_tray.core.models import (
    DEFAULT_TRANSLATION_OPTIONS,
    Chunk,
    ContextBlock,
    Formality,
    IndexedSegment,
    Language,
    PhilologicalNote,
    RateLimitedError,
    TranslationCancelledError,
    TranslationError,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.core.parallel import SharedAttempt, Work, ordered_attempts
from dlx_tray.core.profiles import (
    CLOUD_DEEP_PROFILE,
    CLOUD_STANDARD_PROFILE,
    DEEPL_PROFILE,
    DLX_PROFILE,
    ProviderProfile,
)
from dlx_tray.core.retry import (
    DEFAULT_REQUEST_DELAY,
    RequestDispatch,
    RequestStartClock,
    RetryCallback,
    RetryPolicy,
    Sleeper,
    call_with_retry,
    event_sleep,
    wait_for_request_start,
)
from dlx_tray.providers.base import BlockTranslator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import BlockFallbackTranslator, FallbackTranslator
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
    segment_notes: tuple[tuple[PhilologicalNote, ...], ...] = ()


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
    notes: list[PhilologicalNote] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Piece:
    chunk: Chunk
    segment: _Segment
    first: bool
    last: bool
    cached: TranslationResult | None = None
    request_index: int = 0
    prefix: str = ""


def _preceding_context(segments: Sequence[Chunk], index: int, budget: int) -> tuple[str, ...]:
    context: list[str] = []
    for previous in reversed(segments[max(0, index - 2) : index]):
        if not budget:
            break
        text = previous.text.strip()
        start = len(text)
        while start:
            units = 2 if ord(text[start - 1]) > 0xFFFF else 1
            if units > budget:
                break
            budget -= units
            start -= 1
        if suffix := text[start:].lstrip():
            context.append(suffix)
        if start:
            break
    return tuple(reversed(context))


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslationJob:
    translator: Translator | BlockTranslator
    max_units: int | None = None
    request_delay: float = DEFAULT_REQUEST_DELAY
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Sleeper = event_sleep
    cache: TranslationCache | None = None
    flights: SingleFlight | None = None
    concurrency: int = 1
    diagnostics: DiagnosticTimeline | None = None
    profile: ProviderProfile | None = None
    default_options: TranslationOptions = DEFAULT_TRANSLATION_OPTIONS

    def __post_init__(self) -> None:
        if not 0 <= self.request_delay <= TIMEOUT_MAX:
            raise ValueError(f"request_delay must be between 0 and {TIMEOUT_MAX} seconds")
        limit = chunker.DLX_MAX_UNITS if self._has_dlx else chunker.HARD_MAX_UNITS
        if self.max_units is not None and not chunker.MIN_MAX_UNITS <= self.max_units <= limit:
            raise ValueError("invalid maximum chunk size")
        if self.concurrency not in (1, 2):
            raise ValueError("concurrency must be 1 or 2")
        if self.profile is not None:
            provider = "dlx" if self._has_dlx else self.translator.name
            if self.profile.provider != provider:
                raise ValueError("the provider profile must match the translator")

    def _profile_for(self, options: TranslationOptions) -> ProviderProfile | None:
        if self.profile is not None:
            return self.profile
        if isinstance(self.translator, BlockTranslator):
            if options.analysis_tier == CLOUD_DEEP_PROFILE.analysis_tier:
                return CLOUD_DEEP_PROFILE
            return CLOUD_STANDARD_PROFILE
        if isinstance(self.translator, DeepLOfficialTranslator):
            return DEEPL_PROFILE
        if self._has_dlx:
            return replace(DLX_PROFILE, max_concurrency=self.concurrency)
        return None

    @property
    def chunk_budget(self) -> int:
        profile = self._profile_for(self.default_options)
        return self.max_units or (profile.max_units if profile else chunker.DEFAULT_MAX_UNITS)

    @property
    def uses_segments(self) -> bool:
        return (
            self.cache is not None
            or self.flights is not None
            or isinstance(self.translator, BlockTranslator | DeepLOfficialTranslator)
        )

    @property
    def _has_dlx(self) -> bool:
        return isinstance(self.translator, DeeplxTranslator) or (
            isinstance(self.translator, FallbackTranslator) and self.translator.has_dlx
        )

    @property
    def effective_concurrency(self) -> int:
        profile = self._profile_for(self.default_options)
        return profile.max_concurrency if profile else 1

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
        on_retry_finished: Callable[[], None] | None = None,
        on_provider: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str], None] | None = None,
        file_segment: bool = False,
        pacing: RequestPacing | None = None,
        file_segments: Sequence[str] | None = None,
        on_segment: Callable[[str], None] | None = None,
        options: TranslationOptions | None = None,
    ) -> JobResult:
        """Report progress and partial text after each translated or skipped chunk.

        Continue retries the failed chunk with a fresh retry budget. Skip keeps its source
        text. Without a failure callback, the result carries the error and processed prefix.
        An in-flight request can finish after cancellation and its result is kept.
        """
        options = options if options is not None else self.default_options
        if cancel is None:
            cancel = Event()
        if pacing is None:
            pacing = RequestPacing()
        translator = self.translator
        dlx = self._has_dlx
        block_translator = isinstance(translator, BlockTranslator)
        profile = self._profile_for(options)
        if (
            profile is not None
            and profile.analysis_tier != options.analysis_tier
            and block_translator
        ):
            raise ValueError("the provider profile must match the requested analysis tier")
        max_units = self.max_units or (profile.max_units if profile else chunker.DEFAULT_MAX_UNITS)
        if profile is not None and max_units > profile.max_units:
            raise ValueError("max_units exceeds the provider profile budget")
        concurrency = profile.max_concurrency if profile is not None else self.effective_concurrency
        diagnostics = self.diagnostics
        pacing_sleep = retry_sleep = self.sleep
        if diagnostics is not None:
            pacing_sleep = partial(diagnostics.wait, sleep=self.sleep, kind="pacing")
            retry_sleep = partial(diagnostics.wait, sleep=self.sleep, kind="retry")
        dispatch = RequestDispatch(
            cancel,
            self.request_delay,
            concurrency,
            pacing_sleep,
            clock=RequestStartClock(monotonic) if block_translator else None,
        )
        dlx_batching = dlx and max_units <= chunker.DLX_MAX_UNITS // 2
        batch_size = 2 if dlx_batching else 1
        if profile is not None and (not dlx or dlx_batching):
            batch_size = profile.max_segments
        provider_names = (
            translator.provider_names()
            if isinstance(translator, FallbackTranslator)
            else (translator.name,)
        )
        route = (
            translator.name
            if len(provider_names) == 1 and block_translator
            else repr(provider_names)
        )
        parts: list[str] = []
        providers: list[str | None] = []
        segment_notes: list[tuple[PhilologicalNote, ...]] = []
        detected_source: Language | None = None

        def shared_result(provider: str) -> None:
            if diagnostics is not None:
                diagnostics.append(SharedResult())
            if on_provider is not None:
                on_provider(provider)

        def record(chunk: Chunk, result: TranslationResult | None) -> None:
            nonlocal detected_source
            if result is None:
                parts.append(chunk.text + chunk.separator)
                providers.append(None)
            else:
                parts.append(result.text + chunk.separator)
                providers.append(result.provider)
                detected_source = detected_source or result.detected_source

        def request(pieces: Sequence[_Piece], attempt: RequestDispatch) -> list[TranslationResult]:
            texts = [piece.chunk.text for piece in pieces]
            if isinstance(translator, FallbackTranslator) and not isinstance(
                translator, BlockFallbackTranslator
            ):
                return translator.translate(
                    texts,
                    source,
                    target,
                    formality,
                    cancel=attempt.cancel,
                    on_provider=on_provider,
                    on_rate_limit=on_rate_limit,
                    dispatch=attempt if dlx else None,
                )
            if on_provider is not None and not isinstance(translator, BlockFallbackTranslator):
                on_provider(translator.name)
            try:
                if isinstance(translator, BlockTranslator):
                    assert profile is not None and attempt.clock is not None
                    block = ContextBlock(
                        tuple(
                            IndexedSegment(piece.request_index, piece.chunk.text)
                            for piece in pieces
                        ),
                        _preceding_context(
                            segments, pieces[0].segment.source.index, profile.context_units
                        ),
                    )
                    if isinstance(translator, BlockFallbackTranslator):
                        output = translator.translate_block(
                            block,
                            source,
                            target,
                            options,
                            attempt,
                            formality=formality,
                            on_provider=on_provider,
                            on_rate_limit=on_rate_limit,
                        )
                    else:
                        wait_for_request_start(attempt)
                        output = translator.translate_block(block, source, target, options, attempt)
                    indexed = {item.index: item.result for item in output}
                    if len(output) != len(pieces) or indexed.keys() != {
                        segment.index for segment in block.segments
                    }:
                        raise TranslationError("provider returned mismatched segment indexes")
                    if any(not result.text.strip() for result in indexed.values()):
                        raise TranslationError("provider returned a blank segment translation")
                    return [indexed[segment.index] for segment in block.segments]
                if isinstance(translator, DeeplxTranslator | DeepLOfficialTranslator):
                    return translator.translate(texts, source, target, formality, dispatch=attempt)
                return translator.translate(texts, source, target, formality)
            except RateLimitedError:
                if on_rate_limit is not None and not isinstance(
                    translator, BlockFallbackTranslator
                ):
                    on_rate_limit(translator.name)
                raise

        def request_with_retry(
            pieces: Sequence[_Piece], control: RequestDispatch
        ) -> list[TranslationResult]:
            return call_with_retry(
                lambda: request(pieces, control),
                self.retry_policy,
                cancel=control.cancel,
                sleep=retry_sleep,
                on_retry=on_retry,
                on_retry_finished=on_retry_finished,
            )

        def attempt(piece: _Piece, control: RequestDispatch) -> TranslationResult:
            (result,) = request_with_retry([piece], control)
            return result

        def can_pack(pieces: Sequence[_Piece], candidate: _Piece) -> bool:
            texts = [piece.chunk.text for piece in pieces]
            budget = profile.max_units if dlx and profile is not None else max_units
            return (not dlx or candidate.chunk.text not in texts) and (
                sum(map(chunker.utf16_len, texts)) + chunker.utf16_len(candidate.chunk.text)
                <= budget
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
                        shared_result,
                        provider_pacing=dlx
                        or isinstance(translator, DeepLOfficialTranslator | BlockTranslator),
                    ),
                )

        def report(processed: int) -> None:
            if on_progress is not None:
                on_progress(processed, total, "".join(parts) if file_segments is None else "")

        if file_segments is not None:
            segments = [Chunk(content, "", index) for index, content in enumerate(file_segments)]
        elif self.uses_segments and not file_segment:
            segments = chunker.paragraphs(text)
        else:
            segments = [Chunk(text, "", 0)]
        count_segments = self.uses_segments or file_segments is not None
        legacy_chunks = None if count_segments else chunker.split(text, max_units)
        total = (
            sum(bool(segment.text.strip()) for segment in segments)
            if legacy_chunks is None
            else sum(piece.translatable for piece in legacy_chunks)
        )
        processed = 0
        cache = self.cache
        flights = self.flights

        def plan(scopes: ExitStack) -> Iterator[Work[_Piece]]:
            # Keep original indexes for intact segments. Split continuations get their own IDs.
            continuation_indexes = count(len(segments))
            for segment_index, segment in enumerate(segments):
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
                            translation_key(route, source, target, formality, segment.text, options)
                        )
                    )
                hit = None
                if cache is not None and not cancel.is_set():
                    hit = self._cached_segment(
                        segment.text, source, target, formality, cache, provider_names, options
                    )
                    if diagnostics is not None:
                        diagnostics.append(LocalCache(hit is not None))
                if hit is not None:
                    yield Work(_Piece(segment, group, first=True, last=True, cached=hit))
                    continue
                # Let a serial job store this segment before looking up its adjacent copy.
                next_segment_can_hit_cache = (
                    batch_size > 1
                    and concurrency == 1
                    and cache is not None
                    and segment_index + 1 < len(segments)
                    and segments[segment_index + 1].text == segment.text
                )
                chunks = (
                    legacy_chunks
                    if legacy_chunks is not None
                    else chunker.split_segment(segment.text, max_units)
                )
                if segment.separator:
                    last = chunks[-1]
                    chunks[-1] = replace(last, separator=last.separator + segment.separator)
                prefix = ""
                if block_translator and not chunks[0].translatable:
                    prefix = chunks.pop(0).separator
                keys = {}
                if flights is not None and shared is not None:
                    for chunk in chunks:
                        if chunk.translatable and chunk.text not in keys:
                            key = translation_key(
                                route, source, target, formality, chunk.text, options
                            )
                            flights.reserve(key, shared)
                            keys[chunk.text] = key
                for index, chunk in enumerate(chunks):
                    piece = _Piece(
                        chunk,
                        group,
                        first=index == 0,
                        last=index == len(chunks) - 1,
                        request_index=segment.index if index == 0 else next(continuation_indexes),
                        prefix=prefix if index == 0 else "",
                    )
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

                    yield Work(
                        piece,
                        translate,
                        reservation,
                        pair_with_next=not (piece.last and next_segment_can_hit_cache),
                    )

        def finish_segment(piece: _Piece) -> None:
            group = piece.segment
            notes = tuple(group.notes)
            segment_notes.append(notes)
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
                                provider, source, target, formality, group.source.text, options
                            )
                            cache.store(
                                key, TranslationResult(translated, group.detected, provider, notes)
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
                    ordered_attempts(
                        tasks,
                        dispatch,
                        resolve,
                        shared_result,
                        can_pack=can_pack,
                        batch=request_with_retry if batch_size > 1 else None,
                        batch_size=batch_size,
                        wait_for_first_result=dlx and file_segments is not None,
                    )
                    if (concurrency > 1 or batch_size > 1) and total
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
                        if piece.prefix:
                            parts.append(piece.prefix)
                        record(chunk, result)
                        if result is not None:
                            group.detected = group.detected or result.detected_source
                            group.notes.extend(result.notes)
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
            cancelled = processed < total
        except TranslationError as exc:
            failure = exc
        return JobResult(
            "".join(parts),
            detected_source,
            cancelled,
            failure,
            tuple(providers),
            tuple(segment_notes) if any(segment_notes) else (),
        )

    def _serial_attempt(
        self,
        item: Work[_Piece],
        dispatch: RequestDispatch,
        pacing: RequestPacing,
        cancel: Event,
        on_failure: FailureCallback | None,
        on_shared: Callable[[str], None],
        *,
        provider_pacing: bool,
    ) -> TranslationResult | None:
        attempt = item.attempt
        assert attempt is not None
        started = False

        def run() -> TranslationResult:
            nonlocal started
            if not started and not provider_pacing:
                pacing.before_request(dispatch.delay, dispatch.sleep, cancel)
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
            if not leader:
                on_shared(result.provider)
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
        options: TranslationOptions,
    ) -> TranslationResult | None:
        for provider in provider_names:
            key = translation_key(provider, source, target, formality, text, options)
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
