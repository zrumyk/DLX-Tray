"""Translate captured text in a dialog without replacing the main window's input."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.core.models import TranslationError
from dlx_tray.core.pipeline import FailureAction, JobResult
from dlx_tray.ui.workers import TranslationWorker


class SelectionPopup(QDialog):
    def __init__(self, worker: TranslationWorker | None, status: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Selection translation"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.resize(480, 260)
        self._worker = worker
        self._thread: QThread | None = None
        self._close_result: int | None = None
        self._status = QLabel(self.tr("Translating…") if worker else status)
        self._status.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(self.tr("Translation"))
        self.preview.setVisible(worker is not None)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_button = buttons.addButton(
            self.tr("&Copy"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.copy_button.setEnabled(False)
        self.copy_button.setVisible(worker is not None)
        self.copy_button.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)

        if worker is not None:
            thread = QThread(self)
            self._thread = thread
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.chunk_failed.connect(self._chunk_failed)
            worker.finished.connect(self._finished)
            worker.failed.connect(self._failed)
            worker.history_save_failed.connect(self._history_failed)
            worker.finished.connect(thread.quit)
            worker.failed.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(self._stopped)
            thread.start()

    @Slot(object)
    def _chunk_failed(self, _error: TranslationError) -> None:
        if self._worker is not None:
            self._worker.answer_failure(FailureAction.CANCEL)
        self._failed()

    @Slot(object)
    def _finished(self, result: JobResult) -> None:
        self.preview.setPlainText(result.text)
        self.copy_button.setEnabled(bool(result.text))
        if not result.cancelled:
            self._status.setText(self.tr("Translation complete."))

    @Slot()
    def _failed(self) -> None:
        self._status.setText(self.tr("Translation failed. Check the provider and try again."))

    @Slot()
    def _history_failed(self) -> None:
        self._status.setText(self.tr("Translation complete. Could not save to history."))

    @Slot()
    def _copy(self) -> None:
        QApplication.clipboard().setText(self.preview.toPlainText())

    def done(self, result: int) -> None:
        if self._worker is None:
            super().done(result)
        else:
            self._close_result = result
            if self._worker.cancel():
                self._status.setText(self.tr("Cancelling…"))

    @Slot()
    def _stopped(self) -> None:
        assert self._thread is not None
        self._thread.wait()
        self._thread.deleteLater()
        self._thread = self._worker = None
        if self._close_result is not None:
            super().done(self._close_result)

    def stop_translation(self) -> None:
        if self._worker is not None and self._thread is not None:
            self._worker.cancel()
            self._thread.quit()
            self._thread.wait()
