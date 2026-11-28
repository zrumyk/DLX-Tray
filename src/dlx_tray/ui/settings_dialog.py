"""Edit saved preferences and verify the official DeepL API key."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.config import Settings
from dlx_tray.core.models import AuthError, Formality

log = logging.getLogger(__name__)


class _SettingsOperation(QThread):
    def __init__(self, operation: Callable[[], None], parent: QDialog) -> None:
        super().__init__(parent)
        self._operation: Callable[[], None] | None = operation
        self.error: type[Exception] | None = None

    def run(self) -> None:
        assert self._operation is not None
        try:
            self._operation()
        except Exception as exc:
            log.info("Settings operation failed (%s)", type(exc).__name__)
            self.error = type(exc)
        finally:
            self._operation = None


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        api_key: str | None,
        *,
        key_overridden: bool,
        save: Callable[[Settings, str | None], str | None],
        check_key: Callable[[str], None],
        clear_cache: Callable[[], None] | None = None,
        autostart_registered: bool | None = None,
        set_autostart: Callable[[bool], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Settings"))
        self._settings = settings
        self._override_key = api_key if key_overridden else None
        self._save = save
        self._check_key = check_key
        self._clear_cache = clear_cache
        self._autostart_registered = autostart_registered
        self._set_autostart = set_autostart
        self._operation: _SettingsOperation | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.provider_mode = QComboBox()
        self.provider_mode.addItem(
            self.tr("Automatic (DLX with official API fallback)"), "automatic"
        )
        self.provider_mode.addItem(self.tr("Official DeepL API only"), "official_only")
        self.provider_mode.addItem(self.tr("DLX only"), "dlx_only")
        if settings.provider_mode == "cloud_only":
            self.provider_mode.addItem(self.tr("Cloud AI only (unavailable)"), "cloud_only")
        self.provider_mode.setCurrentIndex(self.provider_mode.findData(settings.provider_mode))
        self._add(form, self.tr("Translation &provider"), self.provider_mode, "provider_mode")

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

        self.dlx_concurrency = QSpinBox()
        self.dlx_concurrency.setRange(1, 2)
        self.dlx_concurrency.setValue(settings.dlx_concurrency)
        self._add(
            form, self.tr("Concurrent DLX &requests"), self.dlx_concurrency, "dlx_concurrency"
        )

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
        form.addRow("", QLabel(self.tr("Restart DLX Tray to apply the interface language.")))

        self.selection_hotkey = QLineEdit(settings.selection_hotkey)
        self._add(form, self.tr("Selection &hotkey"), self.selection_hotkey, "selection_hotkey")
        shortcut_help = QLabel(
            self.tr(
                "Use Ctrl+C,C (copy twice), Ctrl+Shift plus a letter, or leave blank to disable."
            )
        )
        shortcut_help.setWordWrap(True)
        form.addRow("", shortcut_help)

        self.autostart = QCheckBox(self.tr("Start DLX Tray when I sign in to Windows"), self)
        self.autostart.setVisible(set_autostart is not None)
        self.autostart.setEnabled(autostart_registered is not None)
        self.autostart.setChecked(bool(autostart_registered))
        if set_autostart is not None:
            if autostart_registered is None:
                self.autostart.setToolTip(self.tr("Could not read Windows startup settings."))
            form.addRow(self.autostart)

        self.history_enabled = QCheckBox(self.tr("&Enable translation history"))
        self.history_enabled.setChecked(settings.history_enabled)
        self._add(form, "", self.history_enabled, "history_enabled")
        self.history_limit = self._integer_field(settings.history_limit)
        self._add(form, self.tr("&History entry limit"), self.history_limit, "history_limit")
        self.history_enabled.toggled.connect(self._update_history)
        self._update_history(self.history_enabled.isChecked())

        self.cache_enabled = QCheckBox(self.tr("Enable translation c&ache"))
        self.cache_enabled.setChecked(settings.cache_enabled)
        self._add(form, "", self.cache_enabled, "cache_enabled")
        self.clear_cache_button = QPushButton(self.tr("Clear translation cache"))
        self.clear_cache_button.setAutoDefault(False)
        self.clear_cache_button.setEnabled(clear_cache is not None)
        self.clear_cache_button.clicked.connect(self._start_cache_clear)
        form.addRow("", self.clear_cache_button)

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
        self.test_button.setEnabled(has_key and self._operation is None)
        self.key_status.clear()

    @Slot()
    def _start_key_check(self) -> None:
        key = self._effective_key()
        if key is None or self._operation is not None:
            return
        self.key_status.setText(self.tr("Checking the API key…"))
        self._start_operation(partial(self._check_key, key), self._key_check_finished)

    def _start_operation(self, work: Callable[[], None], finished: Callable[[], None]) -> None:
        self._operation = _SettingsOperation(work, self)
        self._operation.finished.connect(finished)
        self.api_key.setEnabled(False)
        self.test_button.setEnabled(False)
        self.clear_cache_button.setEnabled(False)
        self.buttons.setEnabled(False)
        self._operation.start()

    def _finish_operation(self) -> type[Exception] | None:
        operation = self._operation
        assert operation is not None
        operation.wait()
        self._operation = None
        operation.deleteLater()
        self.api_key.setEnabled(True)
        self.test_button.setEnabled(self._effective_key() is not None)
        self.clear_cache_button.setEnabled(self._clear_cache is not None)
        self.buttons.setEnabled(True)
        return operation.error

    @Slot()
    def _key_check_finished(self) -> None:
        error = self._finish_operation()
        if error is None:
            self.key_status.setText(self.tr("DeepL accepted the API key."))
        elif issubclass(error, AuthError):
            self.key_status.setText(self.tr("DeepL rejected the API key."))
        else:
            self.key_status.setText(
                self.tr("Could not verify the key. Check the connection and retry.")
            )

    @Slot()
    def _start_cache_clear(self) -> None:
        clear = self._clear_cache
        if clear is None or self._operation is not None:
            return
        answer = QMessageBox.question(
            self,
            self.tr("Clear translation cache"),
            self.tr("Remove all cached translations?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.save_status.setText(self.tr("Clearing the translation cache…"))
            self._start_operation(clear, self._cache_clear_finished)

    @Slot()
    def _cache_clear_finished(self) -> None:
        error = self._finish_operation()
        self.save_status.setText(
            self.tr("Translation cache cleared.")
            if error is None
            else self.tr("Could not clear the translation cache.")
        )

    def done(self, result: int) -> None:
        if self._operation is None:
            super().done(result)

    def wait_for_operation(self) -> None:
        if self._operation is not None:
            self._operation.wait()

    @Slot()
    def _save_and_close(self) -> None:
        if self._operation is not None:
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
        try:
            settings.selection_hotkey = self.selection_hotkey.text()
        except ValueError:
            self.save_status.setText(self.tr("Enter Ctrl+C,C or Ctrl+Shift plus a letter."))
            self.selection_hotkey.setFocus()
            return
        settings.provider_mode = self.provider_mode.currentData()
        settings.chunk_size = self.chunk_size.value()
        if self.delay.value() != self._initial_delay:
            settings.request_delay = self.delay.value()
        settings.dlx_concurrency = self.dlx_concurrency.value()
        settings.retries = retries
        settings.formality = Formality(self.formality.currentData())
        settings.theme = self.theme.currentData()
        settings.ui_language = self.ui_language.currentData()
        settings.history_enabled = self.history_enabled.isChecked()
        settings.history_limit = history_limit
        settings.cache_enabled = self.cache_enabled.isChecked()
        if error := self._save(settings, self._effective_key()):
            self.save_status.setText(error)
            return
        if self._set_autostart is not None and self._autostart_registered is not None:
            enabled = self.autostart.isChecked()
            if enabled != self._autostart_registered:
                try:
                    self._set_autostart(enabled)
                except (OSError, ValueError) as exc:
                    log.warning(
                        "Could not update Windows startup settings (%s)", type(exc).__name__
                    )
                    self.save_status.setText(
                        self.tr("Preferences saved. Could not update Windows startup settings.")
                    )
                    return
        self.accept()
