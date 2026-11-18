"""Collect translation input and display the result."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from dlx_tray import APP_NAME, __version__
from dlx_tray.core import chunker
from dlx_tray.core.models import Language


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
    ) -> None:
        super().__init__(parent)
        self._chunk_size = chunk_size
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
        self.translate_button.setToolTip(self.tr("Translate (Ctrl+Enter)"))

        languages = QHBoxLayout()
        languages.addWidget(self.source_languages, 1)
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

        self._translate_shortcut = QShortcut(self)
        self._translate_shortcut.setKeys([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        self._translate_shortcut.setAutoRepeat(False)
        self._translate_shortcut.activated.connect(self.translate_button.click)
        self.translate_button.clicked.connect(self._request_translation)
        self.copy_button.clicked.connect(self._copy_result)
        self.swap_button.clicked.connect(self._swap)
        self.source_editor.textChanged.connect(self._update_counter)
        self.result_editor.textChanged.connect(self._update_copy)
        self.source_languages.currentIndexChanged.connect(self._update_swap)
        self._update_counter()
        self._update_copy()
        self._update_swap()
        self.source_editor.setFocus()

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
        chunks = sum(chunk.translatable for chunk in chunker.split(text, self._chunk_size))
        label = self.tr("{chars} characters · {chunks} chunks")
        self.counter.setText(label.format(chars=chars, chunks=chunks))
        self.translate_button.setEnabled(chunks > 0)
        self._translate_shortcut.setEnabled(chunks > 0)

    def _update_copy(self) -> None:
        self.copy_button.setEnabled(not self.result_editor.document().isEmpty())

    def _update_swap(self) -> None:
        enabled = self.source_languages.currentData() != Language.AUTO.value
        self.swap_button.setEnabled(enabled)
        self.swap_button.setToolTip(
            self.tr("Swap languages and texts")
            if enabled
            else self.tr("Choose a source language to swap.")
        )

    def _swap(self) -> None:
        source = self.source_languages.currentData()
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

    def _copy_result(self) -> None:
        QApplication.clipboard().setText(_editor_text(self.result_editor))
