"""Bridge a synchronous translation job to queued Qt signals."""

from __future__ import annotations

import logging
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from dlx_tray.core.models import Formality, Language
from dlx_tray.core.pipeline import TranslationJob

log = logging.getLogger(__name__)


class TranslationWorker(QObject):
    progress = Signal(int, int, str)
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
        self.cancel = Event()

    @Slot()
    def run(self) -> None:
        try:
            result = self._job.run(
                self._text,
                self._source,
                self._target,
                self._formality,
                cancel=self.cancel,
                on_progress=self.progress.emit,
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
