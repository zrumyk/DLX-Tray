"""Bridge a synchronous translation job to queued Qt signals."""

from __future__ import annotations

import logging
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from dlx_tray.core.models import Chunk, Formality, Language, RateLimitedError, TranslationError
from dlx_tray.core.pipeline import FailureAction, TranslationJob
from dlx_tray.providers.deeplx import PROVIDER_NAME as DLX_NAME

log = logging.getLogger(__name__)


class TranslationWorker(QObject):
    progress = Signal(int, int, str)
    provider_changed = Signal(str)
    dlx_rate_limited = Signal(float)
    chunk_failed = Signal(object)
    finished = Signal(object)
    failed = Signal()

    def __init__(
        self,
        job: TranslationJob,
        text: str,
        source: Language,
        target: Language,
        formality: Formality,
    ) -> None:
        super().__init__()
        self._job = job
        self._text = text
        self._source = source
        self._target = target
        self._formality = formality
        self._cancel = Event()
        self._failure_answered = Event()
        self._failure_action = FailureAction.CANCEL

    def cancel(self) -> None:
        self._cancel.set()
        self._failure_answered.set()

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
            result = self._job.run(
                self._text,
                self._source,
                self._target,
                self._formality,
                cancel=self._cancel,
                on_progress=self.progress.emit,
                on_failure=self._ask_about_failure,
                on_retry=self._report_retry,
                on_provider=self.provider_changed.emit,
                on_rate_limit=self._report_rate_limit,
            )
        except Exception as exc:
            # Exception messages from a provider may contain user text or credentials.
            log.error("Unexpected translation worker failure (%s)", type(exc).__name__)
            self.failed.emit()
        else:
            if result.error is None:
                self.finished.emit(result)
            else:
                self.failed.emit()
