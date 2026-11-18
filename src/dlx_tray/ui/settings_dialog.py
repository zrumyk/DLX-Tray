"""Edit saved preferences and verify the official DeepL API key."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QRegularExpression, QThread, Slot
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.config import Settings
from dlx_tray.core.models import AuthError, Formality

log = logging.getLogger(__name__)


class _KeyCheck(QThread):
    def __init__(self, key: str, check: Callable[[str], None], parent: QDialog) -> None:
        super().__init__(parent)
        self._key = key
        self._check = check
        self.error: type[Exception] | None = None

    def run(self) -> None:
        try:
            self._check(self._key)
        except Exception as exc:
            log.info("DeepL key check did not succeed (%s)", type(exc).__name__)
            self.error = type(exc)
        finally:
            self._key = ""


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        api_key: str | None,
        *,
        key_overridden: bool,
        save: Callable[[Settings, str | None], str | None],
        check_key: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Settings"))
        self._settings = settings
        self._override_key = api_key if key_overridden else None
        self._save = save
        self._check_key = check_key
        self._key_check: _KeyCheck | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.chunk_size = QSpinBox()
        self.chunk_size.setRange(200, 1500)
        self.chunk_size.setValue(settings.chunk_size)
        self._add(form, self.tr("&Chunk size (UTF-16 units)"), self.chunk_size, "chunk_size")

        self.delay = QDoubleSpinBox()
        self.delay.setDecimals(3)
        self.delay.setRange(0, 10)
        self.delay.setSingleStep(0.5)
        self.delay.setValue(settings.request_delay)
        self._initial_delay = self.delay.value()
        self._add(form, self.tr("&Delay between requests (s)"), self.delay, "request_delay")

        self.retries = self._integer_field(settings.retries)
        self._add(form, self.tr("&Retries after the first attempt"), self.retries, "retries")

        self.formality = QComboBox()
        for label, value in (
            (self.tr("Default"), Formality.DEFAULT),
            (self.tr("More formal"), Formality.MORE),
            (self.tr("Less formal"), Formality.LESS),
            (self.tr("Prefer more formal"), Formality.PREFER_MORE),
            (self.tr("Prefer less formal"), Formality.PREFER_LESS),
        ):
            self.formality.addItem(label, value.value)
        self.formality.setCurrentIndex(self.formality.findData(settings.formality.value))
        self._add(form, self.tr("&Formality"), self.formality, "formality")
        form.addRow("", QLabel(self.tr("Formality applies to the official DeepL API only.")))

        self.theme = QComboBox()
        self.theme.addItem(self.tr("Follow Windows"), "system")
        self.theme.addItem(self.tr("Light"), "light")
        self.theme.addItem(self.tr("Dark"), "dark")
        self.theme.setCurrentIndex(self.theme.findData(settings.theme))
        self._add(form, self.tr("&Theme"), self.theme, "theme")

        self.ui_language = QComboBox()
        self.ui_language.addItem(self.tr("English"), "en")
        self.ui_language.addItem(self.tr("Ukrainian"), "uk")
        self.ui_language.setCurrentIndex(self.ui_language.findData(settings.ui_language))
        self._add(form, self.tr("Interface &language"), self.ui_language, "ui_language")
        form.addRow("", QLabel(self.tr("Interface language is saved for a future update.")))

        self.history_enabled = QCheckBox(self.tr("&Enable translation history"))
        self.history_enabled.setChecked(settings.history_enabled)
        self._add(form, "", self.history_enabled, "history_enabled")
        self.history_limit = self._integer_field(settings.history_limit)
        self._add(form, self.tr("&History entry limit"), self.history_limit, "history_limit")
        self.history_enabled.toggled.connect(self._update_history)
        self._update_history(self.history_enabled.isChecked())
        form.addRow("", QLabel(self.tr("History storage will be available in a future update.")))

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setAccessibleName(self.tr("DeepL API key"))
        self.api_key.setMaxLength(32767)
        if key_overridden:
            self.api_key.setPlaceholderText(self.tr("Provided by DLX_DEEPL_API_KEY"))
            self.api_key.setReadOnly(True)
        elif api_key is not None:
            self.api_key.setText(api_key)
        self.test_button = QPushButton(self.tr("&Test key"))
        self.test_button.setAutoDefault(False)
        self.test_button.clicked.connect(self._start_key_check)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.test_button)
        key_label = QLabel(self.tr("DeepL API &key"))
        key_label.setBuddy(self.api_key)
        form.addRow(key_label, key_row)
        if not key_overridden:
            form.addRow("", QLabel(self.tr("Clear the field and save to remove the stored key.")))

        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        form.addRow("", self.key_status)
        self.save_status = QLabel()
        self.save_status.setWordWrap(True)
        layout.addWidget(self.save_status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._save_and_close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.api_key.textChanged.connect(self._update_key_controls)
        self._update_key_controls()

    def _add(self, form: QFormLayout, label: str, widget: QWidget, field: str) -> None:
        if self._settings.is_overridden(field):
            widget.setEnabled(False)
            widget.setToolTip(
                self.tr("Set by the {name} environment override.").format(
                    name=f"DLX_{field.upper()}"
                )
            )
        form.addRow(label, widget)

    def _integer_field(self, value: int) -> QLineEdit:
        field = QLineEdit(str(value))
        field.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]+"), field))
        return field

    @Slot(bool)
    def _update_history(self, enabled: bool) -> None:
        if not enabled:
            try:
                valid_limit = int(self.history_limit.text()) >= 1
            except ValueError:
                valid_limit = False
            if not valid_limit:
                self.history_limit.setText(str(self._settings.history_limit))
        self.history_limit.setEnabled(enabled and not self._settings.is_overridden("history_limit"))

    def _effective_key(self) -> str | None:
        return self._override_key or self.api_key.text().strip() or None

    @Slot()
    def _update_key_controls(self) -> None:
        has_key = self._effective_key() is not None
        self.formality.setEnabled(has_key and not self._settings.is_overridden("formality"))
        self.test_button.setEnabled(has_key)
        self.key_status.clear()

    @Slot()
    def _start_key_check(self) -> None:
        key = self._effective_key()
        if key is None or self._key_check is not None:
            return
        self._key_check = _KeyCheck(key, self._check_key, self)
        self._key_check.finished.connect(self._key_check_finished)
        self.api_key.setEnabled(False)
        self.test_button.setEnabled(False)
        self.buttons.setEnabled(False)
        self.key_status.setText(self.tr("Checking the API key…"))
        self._key_check.start()

    @Slot()
    def _key_check_finished(self) -> None:
        check = self._key_check
        assert check is not None
        check.wait()
        self._key_check = None
        check.deleteLater()
        self.api_key.setEnabled(True)
        self.test_button.setEnabled(True)
        self.buttons.setEnabled(True)
        if check.error is None:
            self.key_status.setText(self.tr("DeepL accepted the API key."))
        elif issubclass(check.error, AuthError):
            self.key_status.setText(self.tr("DeepL rejected the API key."))
        else:
            self.key_status.setText(
                self.tr("Could not verify the key. Check the connection and retry.")
            )

    def done(self, result: int) -> None:
        if self._key_check is None:
            super().done(result)

    def wait_for_key_check(self) -> None:
        if self._key_check is not None:
            self._key_check.wait()

    @Slot()
    def _save_and_close(self) -> None:
        if self._key_check is not None:
            return
        try:
            retries = int(self.retries.text())
            history_limit = int(self.history_limit.text())
        except ValueError:
            retries = history_limit = -1
        if retries < 0 or history_limit < 1:
            self.save_status.setText(
                self.tr("Enter a retry count of 0 or more and a history limit of 1 or more.")
            )
            return

        settings = self._settings.model_copy()
        settings.chunk_size = self.chunk_size.value()
        if self.delay.value() != self._initial_delay:
            settings.request_delay = self.delay.value()
        settings.retries = retries
        settings.formality = Formality(self.formality.currentData())
        settings.theme = self.theme.currentData()
        settings.ui_language = self.ui_language.currentData()
        settings.history_enabled = self.history_enabled.isChecked()
        settings.history_limit = history_limit
        if error := self._save(settings, self._effective_key()):
            self.save_status.setText(error)
            return
        self.accept()
