"""Translate captured text in a dialog without replacing the main window's input."""

from __future__ import annotations

from collections.abc import Callable

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

from dlx_tray.core.models import PhilologicalNote, TranslationError
from dlx_tray.core.pipeline import FailureAction, JobResult
from dlx_tray.ui.inspector_panel import note_sections
from dlx_tray.ui.workers import SelectionNotesWorker, TranslationWorker


class SelectionPopup(QDialog):
    def __init__(
        self,
        worker: TranslationWorker | None,
        status: str,
        parent: QWidget,
        *,
        make_notes_worker: Callable[[], SelectionNotesWorker] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Selection translation"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.resize(480, 260)
        self._worker = worker
        self._thread: QThread | None = None
        self._make_notes_worker = make_notes_worker
        self._notes_worker: SelectionNotesWorker | None = None
        self._notes_thread: QThread | None = None
        self._notes_loaded = False
        self._close_result: int | None = None
        self._status = QLabel(self.tr("Translating…") if worker else status)
        self._status.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(self.tr("Translation"))
        self.preview.setVisible(worker is not None)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName(self.tr("Philological notes"))
        self.notes.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_button = buttons.addButton(
            self.tr("&Copy"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.copy_button.setEnabled(False)
        self.copy_button.setVisible(worker is not None)
        self.copy_button.clicked.connect(self._copy)
        self.explain_button = buttons.addButton(
            self.tr("Explain / Notes"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.explain_button.setCheckable(True)
        self.explain_button.setEnabled(False)
        self.explain_button.hide()
        self.explain_button.toggled.connect(self._toggle_notes)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self.preview)
        layout.addWidget(self.notes)
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
        if self._close_result is not None:
            return
        self.preview.setPlainText(result.text)
        self.copy_button.setEnabled(bool(result.text))
        if not result.cancelled:
            self._status.setText(self.tr("Translation complete."))
            if result.segment_notes:
                self._show_notes(result.segment_notes)
            if result.text and (self._notes_loaded or self._make_notes_worker is not None):
                self.explain_button.show()
                self.explain_button.setEnabled(True)

    @Slot(bool)
    def _toggle_notes(self, expanded: bool) -> None:
        self.notes.setVisible(expanded)
        if (
            not expanded
            or self._notes_loaded
            or self._notes_thread is not None
            or self._close_result is not None
        ):
            return
        assert self._make_notes_worker is not None
        self.notes.setPlainText(self.tr("Loading notes…"))
        worker = self._make_notes_worker()
        thread = QThread(self)
        self._notes_worker = worker
        self._notes_thread = thread
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._show_notes)
        worker.failed.connect(self._notes_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._notes_stopped)
        thread.start()

    @Slot(object)
    def _show_notes(self, segments: tuple[tuple[PhilologicalNote, ...], ...] | None) -> None:
        if segments is None or self._close_result is not None:
            return
        self._notes_loaded = True
        sections = note_sections(note for segment in segments for note in segment)
        self.notes.setPlainText(
            "\n\n".join(sections) if sections else self.tr("No notes returned.")
        )

    @Slot(object)
    def _notes_failed(self, _error: type[Exception]) -> None:
        if self._close_result is None:
            self.notes.setPlainText(
                self.tr("Could not load notes. Close and reopen Notes to retry.")
            )

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
        self._close_result = result
        if self._worker is not None:
            self._worker.cancel()
        if self._notes_worker is not None:
            self._notes_worker.cancel()
        if self._thread is not None or self._notes_thread is not None:
            self.explain_button.setEnabled(False)
            self._status.setText(self.tr("Cancelling…"))
        self._finish_close()

    @Slot()
    def _stopped(self) -> None:
        assert self._thread is not None
        self._thread.wait()
        self._thread.deleteLater()
        self._thread = self._worker = None
        self._finish_close()

    @Slot()
    def _notes_stopped(self) -> None:
        assert self._notes_thread is not None
        self._notes_thread.wait()
        self._notes_thread.deleteLater()
        self._notes_thread = self._notes_worker = None
        self._finish_close()

    def _finish_close(self) -> None:
        result = self._close_result
        if result is not None and self._thread is None and self._notes_thread is None:
            super().done(result)

    def stop_workers(self) -> None:
        if self._worker is not None and self._thread is not None:
            self._worker.cancel()
        if self._notes_worker is not None and self._notes_thread is not None:
            self._notes_worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        if self._notes_thread is not None:
            self._notes_thread.quit()
            self._notes_thread.wait()
