"""Bridge a synchronous translation job to queued Qt signals."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Event, Lock

from PySide6.QtCore import QObject, Signal, Slot

from dlx_tray.core import chunker
from dlx_tray.core.models import Chunk, Formality, Language, RateLimitedError, TranslationError
from dlx_tray.core.pipeline import (
    FailureAction,
    JobResult,
    ProgressCallback,
    RequestPacing,
    TranslationJob,
)
from dlx_tray.fileio.translation import SourceFile
from dlx_tray.providers.deeplx import PROVIDER_NAME as DLX_NAME
from dlx_tray.services.history import HistoryStore

log = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class FileJobResult:
    preview: JobResult
    translations: tuple[str, ...] | None


class TranslationWorker(QObject):
    progress = Signal(int, int, str)
    provider_changed = Signal(str)
    dlx_rate_limited = Signal(float)
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
            self.dlx_rate_limited.emit(0.0)

    def _report_retry(self, _attempt: int, delay: float, error: TranslationError) -> None:
        if self._job.translator.name == DLX_NAME and isinstance(error, RateLimitedError):
            self.dlx_rate_limited.emit(delay)

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
                source_text="\n\n".join(content.segments)
                if isinstance(content, SourceFile)
                else content,
                target_text=result.text,
                provider=", ".join(providers),
                file_name=content.path.name if isinstance(content, SourceFile) else None,
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
            on_provider=self.provider_changed.emit,
            on_rate_limit=self._report_rate_limit,
            file_segment=file_pacing is not None,
            pacing=file_pacing,
        )

    def _translate_file(self, source: SourceFile) -> FileJobResult:
        counts = [
            int(bool(text.strip()))
            if self._job.uses_segments
            else sum(chunk.translatable for chunk in chunker.split(text, self._job.max_units))
            for text in source.segments
        ]
        total = sum(counts)
        offset = 0
        self.progress.emit(0, total, "")
        translations: list[str] = []
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
            if result.text or not result.cancelled:
                translations.append(result.text)
            detected = detected or result.detected_source
            error = result.error
            if result.cancelled:
                self._cancel.set()
                break
            if error is not None:
                break
            offset += count
        cancelled = error is None and (self._cancel.is_set() or offset < total)
        preview = JobResult("\n\n".join(translations), detected, cancelled, error, tuple(providers))
        return FileJobResult(preview, None if cancelled or error else tuple(translations))
