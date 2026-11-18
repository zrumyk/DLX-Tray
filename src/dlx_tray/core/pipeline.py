"""Run a cancellable, synchronous translation job one chunk at a time."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import TIMEOUT_MAX, Event
from typing import TYPE_CHECKING

from dlx_tray.core import chunker
from dlx_tray.core.models import (
    Chunk,
    Formality,
    Language,
    TranslationCancelledError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.retry import RetryCallback, RetryPolicy, Sleeper, call_with_retry, event_sleep
from dlx_tray.providers.fallback import FallbackTranslator

if TYPE_CHECKING:
    from dlx_tray.providers.base import Translator

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
    # One entry per processed translatable chunk, with None for a skipped chunk.
    chunk_providers: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslationJob:
    translator: Translator
    max_units: int = chunker.DEFAULT_MAX_UNITS
    request_delay: float = 1.5
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Sleeper = event_sleep

    def __post_init__(self) -> None:
        if not 0 <= self.request_delay <= TIMEOUT_MAX:
            raise ValueError(f"request_delay must be between 0 and {TIMEOUT_MAX} seconds")

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
    ) -> JobResult:
        """Report (processed, total, partial text) after each translated or skipped chunk.

        Continue retries the failed chunk with a fresh retry budget. Skip keeps its source
        text. Without a failure callback, the result carries the error and processed prefix.
        An in-flight request can finish after cancellation and its result is kept.
        """
        if cancel is None:
            cancel = Event()
        chunks = chunker.split(text, self.max_units)
        translations: list[str] = []
        chunk_providers: list[str | None] = []
        if chunks and not chunks[0].translatable:
            translations.append("")
        first = len(translations)
        total = len(chunks) - first
        detected_source: Language | None = None
        cancelled = False
        failure: TranslationError | None = None

        for processed, chunk in enumerate(chunks[first:], start=1):
            if processed > 1 and self.request_delay and not self.sleep(self.request_delay, cancel):
                cancelled = True
                break

            def translate(chunk_text: str = chunk.text) -> TranslationResult:
                if isinstance(self.translator, FallbackTranslator):
                    (result,) = self.translator.translate(
                        [chunk_text], source, target, formality, cancel=cancel
                    )
                else:
                    (result,) = self.translator.translate([chunk_text], source, target, formality)
                return result

            try:
                result = self._translate_chunk(translate, chunk, cancel, on_failure, on_retry)
            except TranslationCancelledError:
                cancelled = True
                break
            except TranslationError as exc:
                failure = exc
                break

            if result is None:
                translations.append(chunk.text)
                chunk_providers.append(None)
            else:
                translations.append(result.text)
                chunk_providers.append(result.provider)
                if detected_source is None:
                    detected_source = result.detected_source
            if on_progress is not None:
                partial = chunker.join(chunks[: len(translations)], translations)
                on_progress(processed, total, partial)

        return JobResult(
            chunker.join(chunks[: len(translations)], translations),
            detected_source,
            cancelled=cancelled,
            error=failure,
            chunk_providers=tuple(chunk_providers),
        )

    def _translate_chunk(
        self,
        translate: Callable[[], TranslationResult],
        chunk: Chunk,
        cancel: Event,
        on_failure: FailureCallback | None,
        on_retry: RetryCallback | None,
    ) -> TranslationResult | None:
        while True:
            try:
                return call_with_retry(
                    translate,
                    self.retry_policy,
                    cancel=cancel,
                    sleep=self.sleep,
                    on_retry=on_retry,
                )
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
