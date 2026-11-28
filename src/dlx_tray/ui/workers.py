"""Bridge a synchronous translation job to queued Qt signals."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Event, Lock, get_ident
from time import monotonic

from PySide6.QtCore import QObject, Signal, Slot

from dlx_tray.contracts.cloud_v1 import EXTRACT_TERMS_MAX_UNITS
from dlx_tray.core import chunker
from dlx_tray.core.models import (
    AnalysisTier,
    Chunk,
    CloudDeepTierForbiddenError,
    Formality,
    Language,
    TranslationCancelledError,
    TranslationError,
)
from dlx_tray.core.pipeline import (
    FailureAction,
    JobResult,
    ProgressCallback,
    RequestPacing,
    TranslationJob,
)
from dlx_tray.fileio.translation import SourceFile
from dlx_tray.providers.cloud_ai import CloudAiTranslator
from dlx_tray.providers.deeplx import PROVIDER_NAME as DLX_NAME
from dlx_tray.services.history import HistoryStore
from dlx_tray.services.term_extraction import extraction_request

log = logging.getLogger(__name__)


class SelectionNotesWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        job: TranslationJob,
        cloud: CloudAiTranslator,
        text: str,
        source: Language,
        target: Language,
    ) -> None:
        super().__init__()
        self._job = job
        self._cloud = cloud
        self._text = text
        self._source = source
        self._target = target
        self._cancel = Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        options = self._job.default_options
        tier = options.analysis_tier

        def request(tier: AnalysisTier) -> JobResult:
            return self._job.run(
                self._text,
                self._source,
                self._target,
                cancel=self._cancel,
                options=replace(options, analysis_tier=tier),
            )

        try:
            if (
                tier is AnalysisTier.DEEP
                and not self._cloud.check_token(cancel=self._cancel).allow_deep_tier
            ):
                tier = AnalysisTier.STANDARD
            result = request(tier)
            if isinstance(result.error, CloudDeepTierForbiddenError) and tier is AnalysisTier.DEEP:
                result = request(AnalysisTier.STANDARD)
        except TranslationCancelledError:
            self.finished.emit(None)
        except Exception as exc:
            log.warning("Selection notes failed (%s)", type(exc).__name__)
            self.failed.emit(type(exc))
        else:
            if result.error is not None:
                self.failed.emit(type(result.error))
            else:
                self.finished.emit(None if self._cancel.is_set() else result.segment_notes)


class FileIOWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, operation: Callable[[], SourceFile | None]) -> None:
        super().__init__()
        self._operation = operation

    @Slot()
    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as exc:
            log.warning("File operation failed (%s)", type(exc).__name__)
            self.failed.emit(type(exc))
        else:
            self.finished.emit(result)


class TermExtractionWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(
        self,
        cloud: CloudAiTranslator,
        content: str | SourceFile,
        source: Language,
        target: Language,
        budget: int = EXTRACT_TERMS_MAX_UNITS,
    ) -> None:
        super().__init__()
        self._cloud = cloud
        self._content = content
        self._source = source
        self._target = target
        self._budget = budget
        self._cancel = Event()

    def cancel(self) -> bool:
        self._cancel.set()
        return True

    @Slot()
    def run(self) -> None:
        try:
            segments = (
                self._content.segments
                if isinstance(self._content, SourceFile)
                else (piece.text for piece in chunker.paragraphs(self._content))
            )
            request = extraction_request(
                segments, self._source, self._target, budget=self._budget, cancel=self._cancel
            )
            response = self._cloud.extract_terms(request, cancel=self._cancel)
        except TranslationCancelledError:
            self.finished.emit(None)
        except Exception as exc:
            log.warning("Term extraction failed (%s)", type(exc).__name__)
            self.failed.emit(type(exc))
        else:
            self.finished.emit(None if self._cancel.is_set() else response)


@dataclass(frozen=True, slots=True)
class FileJobResult:
    preview: JobResult
    translations: tuple[str, ...] | None


class TranslationWorker(QObject):
    progress = Signal(int, int, str)
    provider_changed = Signal(str)
    dlx_rate_limited = Signal()
    retry_wait_started = Signal(object, float, object)
    retry_wait_finished = Signal(object)
    chunk_failed = Signal(object)
    finished = Signal(object)
    failed = Signal()
    history_saving = Signal()
    history_save_failed = Signal()

    def __init__(
        self,
        job: TranslationJob,
        content: str | SourceFile,
        source: Language,
        target: Language,
        formality: Formality,
        open_history: Callable[[], HistoryStore] | None = None,
    ) -> None:
        super().__init__()
        self._job = job
        self._content = content
        self._source = source
        self._target = target
        self._formality = formality
        self._open_history = open_history
        self._cancel = Event()
        self._completion_lock = Lock()
        self._finalizing = False
        self._failure_answered = Event()
        self._failure_action = FailureAction.CANCEL

    def cancel(self) -> bool:
        with self._completion_lock:
            if self._finalizing:
                return False
            self._cancel.set()
            self._failure_answered.set()
            return True

    def answer_failure(self, action: FailureAction) -> None:
        # The GUI calls this directly because run() occupies the worker thread.
        self._failure_action = action
        self._failure_answered.set()

    def _ask_about_failure(self, _chunk: Chunk, error: TranslationError) -> FailureAction:
        self._failure_answered.clear()
        if self._cancel.is_set():
            return FailureAction.CANCEL
        self.chunk_failed.emit(error)
        self._failure_answered.wait()
        return self._failure_action

    def _report_rate_limit(self, provider: str) -> None:
        if provider == DLX_NAME:
            self.dlx_rate_limited.emit()

    def _report_retry(self, _attempt: int, delay: float, error: TranslationError) -> None:
        if delay > 0:
            self.retry_wait_started.emit(get_ident(), monotonic() + delay, type(error))

    def _finish_retry_wait(self) -> None:
        self.retry_wait_finished.emit(get_ident())

    @Slot()
    def run(self) -> None:
        try:
            result = (
                self._translate_file(self._content)
                if isinstance(self._content, SourceFile)
                else self._translate_text(self._content, self.progress.emit)
            )
        except Exception as exc:
            # Exception messages from a provider may contain user text or credentials.
            log.error("Unexpected translation worker failure (%s)", type(exc).__name__)
            self.failed.emit()
        else:
            preview = result.preview if isinstance(result, FileJobResult) else result
            if preview.error is None:
                with self._completion_lock:
                    self._finalizing = True
                    cancelled = self._cancel.is_set()
                if cancelled and not preview.cancelled:
                    preview = replace(preview, cancelled=True)
                    result = (
                        FileJobResult(preview, None)
                        if isinstance(result, FileJobResult)
                        else preview
                    )
                history_saved = self._save_history(preview)
                self.finished.emit(result)
                if not history_saved:
                    self.history_save_failed.emit()
            else:
                self.failed.emit()

    def _save_history(self, result: JobResult) -> bool:
        if self._open_history is None or result.cancelled:
            return True
        providers = dict.fromkeys(name for name in result.chunk_providers if name is not None)
        if not providers:
            return True
        self.history_saving.emit()
        content = self._content
        try:
            self._open_history().add(
                source_lang=self._source,
                target_lang=self._target,
                source_text=content.assemble(content.segments)
                if isinstance(content, SourceFile)
                else content,
                target_text=result.text,
                provider=", ".join(providers),
                file_name=content.path.name if isinstance(content, SourceFile) else None,
                annotations=result.annotations,
            )
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.warning("Could not save translation history (%s)", type(exc).__name__)
            return False
        return True

    def _translate_text(
        self, text: str, on_progress: ProgressCallback, *, file_pacing: RequestPacing | None = None
    ) -> JobResult:
        return self._job.run(
            text,
            self._source,
            self._target,
            self._formality,
            cancel=self._cancel,
            on_progress=on_progress,
            on_failure=self._ask_about_failure,
            on_retry=self._report_retry,
            on_retry_finished=self._finish_retry_wait,
            on_provider=self.provider_changed.emit,
            on_rate_limit=self._report_rate_limit,
            file_segment=file_pacing is not None,
            pacing=file_pacing,
        )

    def _translate_file(self, source: SourceFile) -> FileJobResult:
        if self._job.uses_segments:
            return self._translate_file_segments(source)
        counts = [
            sum(chunk.translatable for chunk in chunker.split(text, self._job.chunk_budget))
            for text in source.segments
        ]
        total = sum(counts)
        offset = 0
        self.progress.emit(0, total, "")
        translations: list[str] = []
        partial = ""
        providers: list[str | None] = []
        detected: Language | None = None
        error: TranslationError | None = None
        pacing = RequestPacing()

        def report(processed: int, _total: int, _text: str) -> None:
            self.progress.emit(offset + processed, total, "")

        for text, count in zip(source.segments, counts, strict=True):
            if self._cancel.is_set():
                break
            result = self._translate_text(text, report, file_pacing=pacing)
            providers.extend(result.chunk_providers)
            detected = detected or result.detected_source
            error = result.error
            if result.cancelled:
                partial = result.text
                self._cancel.set()
                break
            if error is not None:
                break
            translations.append(result.text)
            offset += count
        cancelled = error is None and (self._cancel.is_set() or offset < total)
        preview = JobResult(
            source.assemble(translations, partial=partial),
            detected,
            cancelled,
            error,
            tuple(providers),
        )
        return FileJobResult(preview, None if cancelled or error else tuple(translations))

    def _translate_file_segments(self, source: SourceFile) -> FileJobResult:
        translations: list[str] = []
        total = sum(bool(text.strip()) for text in source.segments)
        self.progress.emit(0, total, "")

        def report(processed: int, total: int, _text: str) -> None:
            self.progress.emit(processed, total, "")

        preview = self._job.run(
            "",
            self._source,
            self._target,
            self._formality,
            file_segments=source.segments,
            on_segment=translations.append,
            cancel=self._cancel,
            on_progress=report,
            on_failure=self._ask_about_failure,
            on_retry=self._report_retry,
            on_retry_finished=self._finish_retry_wait,
            on_provider=self.provider_changed.emit,
            on_rate_limit=self._report_rate_limit,
        )
        partial = preview.text[sum(map(len, translations)) :]
        preview_text = source.assemble(translations, partial=partial)
        annotations = preview.annotations
        if annotations is not None:
            offset = len(source.txt_prefix)
            segments = []
            for index, segment in enumerate(annotations.segments):
                segments.append(
                    replace(
                        segment,
                        source_start=segment.source_start + offset,
                        target_start=segment.target_start + offset,
                    )
                )
                offset += (
                    len(source.txt_separators[index]) if source.txt_separators is not None else 2
                )
            annotations = replace(annotations, segments=tuple(segments))
        preview = replace(preview, text=preview_text, annotations=annotations)
        return FileJobResult(
            preview, None if preview.cancelled or preview.error else tuple(translations)
        )
