"""Collect translation input and display the result."""

from __future__ import annotations

from math import ceil

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QWidget,
)

from dlx_tray import APP_NAME, __version__
from dlx_tray.core import chunker
from dlx_tray.core.models import Formality, Language, TranslationError
from dlx_tray.core.pipeline import JobResult, TranslationJob
from dlx_tray.providers.deepl_official import PROVIDER_NAME as DEEPL_NAME
from dlx_tray.providers.deeplx import PROVIDER_NAME as DLX_NAME
from dlx_tray.ui.error_dialog import ErrorDialog
from dlx_tray.ui.workers import TranslationWorker


def _editor_text(editor: QPlainTextEdit) -> str:
    # QTextDocument.toPlainText() turns non-breaking spaces into ordinary spaces.
    return editor.document().toRawText().replace("\u2029", "\n")


class MainWindow(QMainWindow):
    translation_requested = Signal(str, object, object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        source: Language = Language.AUTO,
        target: Language = Language.UK,
        chunk_size: int = chunker.DEFAULT_MAX_UNITS,
        translation_job: TranslationJob | None = None,
        formality: Formality = Formality.DEFAULT,
        translation_unavailable_reason: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._chunk_size = translation_job.max_units if translation_job else chunk_size
        self._job = translation_job
        self._formality = formality
        self._translation_unavailable_reason = translation_unavailable_reason
        self._thread: QThread | None = None
        self._worker: TranslationWorker | None = None
        self._error_dialog: ErrorDialog | None = None
        self._detected_source: Language | None = None
        self._close_pending = False
        self._rendered_chars = 0
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1100, 700)

        central = QWidget(self)
        layout = QGridLayout(central)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(1, 1)
        self.setCentralWidget(central)

        self.source_languages = QComboBox()
        self.source_languages.setAccessibleName(self.tr("Source language"))
        self.target_languages = QComboBox()
        self.target_languages.setAccessibleName(self.tr("Target language"))
        self._populate_languages(source, target)
        self.detected_language = QLabel()
        self.detected_language.hide()

        self.swap_button = QPushButton(self.tr("Swap"))
        self.swap_button.setAccessibleName(self.tr("Swap languages and texts"))
        self.source_editor = QPlainTextEdit()
        self.source_editor.setAccessibleName(self.tr("Source text"))
        self.source_editor.setPlaceholderText(self.tr("Enter text to translate"))
        self.result_editor = QPlainTextEdit()
        self.result_editor.setAccessibleName(self.tr("Translation result"))
        self.result_editor.setPlaceholderText(self.tr("Translation"))
        self.result_editor.setReadOnly(True)
        self.counter = QLabel()
        self.counter.setToolTip(self.tr("Characters are counted in UTF-16 code units."))
        self.copy_button = QPushButton(self.tr("Copy result"))
        self.translate_button = QPushButton(self.tr("Translate"))
        self.translate_button.setToolTip(
            translation_unavailable_reason or self.tr("Translate (Ctrl+Enter)")
        )
        if translation_unavailable_reason:
            self.statusBar().showMessage(translation_unavailable_reason)
        self._dlx_status = QLabel()
        self._dlx_status.setObjectName("dlx_status")
        self._dlx_status.hide()
        self.statusBar().addPermanentWidget(self._dlx_status)
        self._provider_status = QLabel()
        self._provider_status.setObjectName("provider_status")
        self._provider_status.hide()
        self.statusBar().addPermanentWidget(self._provider_status)
        self._rate_limit_status = QLabel()
        self._rate_limit_status.setObjectName("rate_limit_status")
        self._rate_limit_status.hide()
        self.statusBar().addPermanentWidget(self._rate_limit_status)

        languages = QHBoxLayout()
        source_controls = QHBoxLayout()
        source_controls.addWidget(self.source_languages, 1)
        source_controls.addWidget(self.detected_language)
        languages.addLayout(source_controls, 1)
        languages.addWidget(self.swap_button)
        languages.addWidget(self.target_languages, 1)
        layout.addLayout(languages, 0, 0, 1, 2)

        layout.addWidget(self.source_editor, 1, 0)
        layout.addWidget(self.result_editor, 1, 1)

        source_actions = QHBoxLayout()
        source_actions.addWidget(self.counter)
        source_actions.addStretch()
        source_actions.addWidget(self.translate_button)
        layout.addLayout(source_actions, 2, 0)
        layout.addWidget(self.copy_button, 2, 1, alignment=Qt.AlignmentFlag.AlignRight)

        self._progress_row = QWidget()
        progress_layout = QHBoxLayout(self._progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_label = QLabel()
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName(self.tr("Translation progress"))
        self.progress_bar.setTextVisible(False)
        self.cancel_button = QPushButton(self.tr("Cancel"))
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)
        progress_layout.addWidget(self.cancel_button)
        layout.addWidget(self._progress_row, 3, 0, 1, 2)
        self._progress_row.hide()

        self._translate_shortcut = QShortcut(self)
        self._translate_shortcut.setKeys([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        self._translate_shortcut.setAutoRepeat(False)
        self._translate_shortcut.activated.connect(self.translate_button.click)
        self.translate_button.clicked.connect(self._request_translation)
        self.translation_requested.connect(self._start_translation)
        self.cancel_button.clicked.connect(self._cancel_translation)
        self.copy_button.clicked.connect(self._copy_result)
        self.swap_button.clicked.connect(self._swap)
        self.source_editor.textChanged.connect(self._update_counter)
        self.source_editor.textChanged.connect(self._clear_detected_source)
        self.result_editor.textChanged.connect(self._update_copy)
        self.source_languages.currentIndexChanged.connect(self._clear_detected_source)
        self.target_languages.currentIndexChanged.connect(self._clear_detected_source)
        self._update_counter()
        self._update_copy()
        self._update_swap()
        self.source_editor.setFocus()

    def set_dlx_connection(self, status: str, job: TranslationJob | None = None) -> None:
        """Show the startup outcome and enable translation when a job is available."""
        self._job = job
        self._translation_unavailable_reason = status if job is None else None
        self._dlx_status.setText(status)
        self._dlx_status.show()
        self.translate_button.setToolTip(
            self._translation_unavailable_reason or self.tr("Translate (Ctrl+Enter)")
        )
        if job is not None:
            self._chunk_size = job.max_units
        self._update_counter()

    def _populate_languages(self, source: Language, target: Language) -> None:
        names = {
            Language.AR: self.tr("Arabic"),
            Language.BG: self.tr("Bulgarian"),
            Language.CS: self.tr("Czech"),
            Language.DA: self.tr("Danish"),
            Language.DE: self.tr("German"),
            Language.EL: self.tr("Greek"),
            Language.EN: self.tr("English"),
            Language.EN_GB: self.tr("English (British)"),
            Language.EN_US: self.tr("English (American)"),
            Language.ES: self.tr("Spanish"),
            Language.ET: self.tr("Estonian"),
            Language.FI: self.tr("Finnish"),
            Language.FR: self.tr("French"),
            Language.HU: self.tr("Hungarian"),
            Language.ID: self.tr("Indonesian"),
            Language.IT: self.tr("Italian"),
            Language.JA: self.tr("Japanese"),
            Language.KO: self.tr("Korean"),
            Language.LT: self.tr("Lithuanian"),
            Language.LV: self.tr("Latvian"),
            Language.NB: self.tr("Norwegian Bokmål"),
            Language.NL: self.tr("Dutch"),
            Language.PL: self.tr("Polish"),
            Language.PT: self.tr("Portuguese"),
            Language.PT_BR: self.tr("Portuguese (Brazilian)"),
            Language.PT_PT: self.tr("Portuguese (European)"),
            Language.RO: self.tr("Romanian"),
            Language.RU: self.tr("Russian"),
            Language.SK: self.tr("Slovak"),
            Language.SL: self.tr("Slovenian"),
            Language.SV: self.tr("Swedish"),
            Language.TR: self.tr("Turkish"),
            Language.UK: self.tr("Ukrainian"),
            Language.ZH: self.tr("Chinese"),
            Language.ZH_HANS: self.tr("Chinese (Simplified)"),
            Language.ZH_HANT: self.tr("Chinese (Traditional)"),
        }
        self.source_languages.addItem(self.tr("Auto-detect"), Language.AUTO.value)
        for language, name in sorted(names.items(), key=lambda item: item[1].casefold()):
            if language.can_be_source:
                self.source_languages.addItem(name, language.value)
            self.target_languages.addItem(name, language.value)
        self.source_languages.setCurrentIndex(self.source_languages.findData(source.value))
        self.target_languages.setCurrentIndex(self.target_languages.findData(target.value))

    def _update_counter(self) -> None:
        text = _editor_text(self.source_editor)
        chars = chunker.utf16_len(text)
        self._chunk_count = sum(
            chunk.translatable for chunk in chunker.split(text, self._chunk_size)
        )
        label = self.tr("{chars} characters · {chunks} chunks")
        self.counter.setText(label.format(chars=chars, chunks=self._chunk_count))
        self._update_translate()

    def _update_translate(self) -> None:
        enabled = (
            self._translation_unavailable_reason is None
            and self._thread is None
            and self._chunk_count > 0
        )
        self.translate_button.setEnabled(enabled)
        self._translate_shortcut.setEnabled(enabled)

    def _update_copy(self) -> None:
        self.copy_button.setEnabled(not self.result_editor.document().isEmpty())

    def _update_swap(self) -> None:
        explicit_source = self.source_languages.currentData() != Language.AUTO.value
        enabled = explicit_source or self._detected_source is not None
        self.swap_button.setEnabled(enabled and self._thread is None)
        self.swap_button.setToolTip(
            self.tr("Swap languages and texts")
            if enabled
            else self.tr("Translate to detect the source language, or choose it to swap.")
        )

    def _clear_detected_source(self) -> None:
        self._detected_source = None
        self.detected_language.hide()
        self._update_swap()

    def _swap(self) -> None:
        source = self.source_languages.currentData()
        if source == Language.AUTO.value:
            if self._detected_source is None:
                return
            source = self._detected_source.value
        target = self.target_languages.currentData().partition("-")[0]
        self.source_languages.setCurrentIndex(self.source_languages.findData(target))
        self.target_languages.setCurrentIndex(self.target_languages.findData(source))
        if not self.result_editor.document().isEmpty():
            source_text = _editor_text(self.source_editor)
            self.source_editor.setPlainText(_editor_text(self.result_editor))
            self.result_editor.setPlainText(source_text)

    def _request_translation(self) -> None:
        self.translation_requested.emit(
            _editor_text(self.source_editor),
            Language(self.source_languages.currentData()),
            Language(self.target_languages.currentData()),
        )

    @Slot(str, object, object)
    def _start_translation(self, text: str, source: Language, target: Language) -> None:
        if self._job is None or self._thread is not None or not text.strip():
            return

        thread = QThread(self)
        worker = TranslationWorker(self._job, text, source, target, self._formality)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._show_progress)
        worker.provider_changed.connect(self._show_provider)
        worker.dlx_rate_limited.connect(self._show_dlx_rate_limit)
        worker.chunk_failed.connect(self._show_chunk_failure)
        worker.finished.connect(self._show_finished)
        worker.failed.connect(self._show_failure)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._translation_stopped)

        self._rendered_chars = 0
        self._clear_detected_source()
        self.result_editor.clear()
        self.statusBar().clearMessage()
        self._provider_status.hide()
        self._rate_limit_status.hide()
        self.progress_bar.setRange(0, self._chunk_count)
        self.progress_bar.setValue(0)
        self.progress_label.setText(self.tr("Starting translation…"))
        self.cancel_button.setEnabled(True)
        self._progress_row.show()
        self._set_input_locked(True)
        thread.start()

    def _set_input_locked(self, locked: bool) -> None:
        self.source_editor.setReadOnly(locked)
        self.source_languages.setEnabled(not locked)
        self.target_languages.setEnabled(not locked)
        self._update_translate()
        self._update_swap()

    @Slot(int, int, str)
    def _show_progress(self, processed: int, total: int, text: str) -> None:
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(processed)
        if self.cancel_button.isEnabled():
            label = self.tr("Chunk {processed} of {total}")
            self.progress_label.setText(label.format(processed=processed, total=total))
        self._append_result(text)

    @Slot(str)
    def _show_provider(self, provider: str) -> None:
        names = {DLX_NAME: "DLX", DEEPL_NAME: self.tr("DeepL API")}
        self._provider_status.setText(
            self.tr("Provider: {name}").format(name=names.get(provider, provider))
        )
        self._provider_status.show()
        if not self._rate_limit_status.isHidden():
            self._show_dlx_rate_limit(0.0)

    @Slot(float)
    def _show_dlx_rate_limit(self, retry_delay: float) -> None:
        warning = self.tr("DLX rate limit reached.")
        if retry_delay > 0:
            warning = self.tr("DLX rate limit reached. Retrying in {seconds} s.").format(
                seconds=ceil(retry_delay)
            )
        self._rate_limit_status.setText(warning)
        self._rate_limit_status.show()

    def _append_result(self, text: str) -> None:
        if len(text) > self._rendered_chars:
            cursor = QTextCursor(self.result_editor.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text[self._rendered_chars :])
            self._rendered_chars = len(text)

    @Slot(object)
    def _show_finished(self, result: JobResult) -> None:
        self._append_result(result.text)
        detected = result.detected_source
        if (
            not result.cancelled
            and self.source_languages.currentData() == Language.AUTO.value
            and detected is not None
            and detected.can_be_target
        ):
            self._detected_source = detected
            index = self.target_languages.findData(detected.value)
            label = self.tr("Detected: {language}")
            self.detected_language.setText(
                label.format(language=self.target_languages.itemText(index))
            )
            self.detected_language.show()
        self.statusBar().showMessage(
            self.tr("Translation cancelled.")
            if result.cancelled
            else self.tr("Translation complete.")
        )

    @Slot()
    def _show_failure(self) -> None:
        self.statusBar().showMessage(self.tr("Translation failed. Check DLX and try again."))

    @Slot(object)
    def _show_chunk_failure(self, error: TranslationError) -> None:
        if self._worker is None or not self.cancel_button.isEnabled():
            return
        dialog = ErrorDialog(
            self.progress_bar.value() + 1, self.progress_bar.maximum(), error, self
        )
        self._error_dialog = dialog
        dialog.finished.connect(self._answer_chunk_failure)
        dialog.show()

    @Slot()
    def _answer_chunk_failure(self) -> None:
        dialog = self._error_dialog
        assert dialog is not None and self._worker is not None
        self._error_dialog = None
        self._worker.answer_failure(dialog.action)
        dialog.deleteLater()

    @Slot()
    def _cancel_translation(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText(self.tr("Cancelling…"))
            if self._error_dialog is not None:
                self._error_dialog.reject()

    @Slot()
    def _translation_stopped(self) -> None:
        thread = self._thread
        assert thread is not None
        thread.wait()
        thread.deleteLater()
        self._thread = None
        self._worker = None
        self._progress_row.hide()
        if not self._rate_limit_status.isHidden():
            self._show_dlx_rate_limit(0.0)
        self._set_input_locked(False)
        if self._close_pending:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None:
            self._close_pending = True
            self._cancel_translation()
            event.ignore()
            return
        super().closeEvent(event)

    def stop_translation(self) -> None:
        """Join an active worker if the application event loop has exited."""
        if self._thread is not None:
            self._cancel_translation()
            self._thread.quit()
            self._thread.wait()

    def _copy_result(self) -> None:
        QApplication.clipboard().setText(_editor_text(self.result_editor))
