"""Exercise settings controls and the background DeepL key check."""

from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit
from pytestqt.qtbot import QtBot

from dlx_tray.config import Settings
from dlx_tray.core.models import AuthError, ProviderUnavailableError
from dlx_tray.ui.settings_dialog import SettingsDialog


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (None, "DeepL accepted the API key."),
        (AuthError, "DeepL rejected the API key."),
        (ProviderUnavailableError, "Could not verify the key. Check the connection and retry."),
    ],
)
def test_key_check_runs_in_background(
    qtbot: QtBot, error: type[Exception] | None, message: str
) -> None:
    release = Event()
    entered = Event()

    def check(key: str) -> None:
        assert key == "test-key"
        entered.set()
        assert release.wait(timeout=5)
        if error:
            raise error("sensitive detail")

    save = Mock()
    dialog = SettingsDialog(Settings(), None, key_overridden=False, save=save, check_key=check)
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.formality.isEnabled()
    assert not dialog.test_button.isEnabled()
    assert dialog.api_key.echoMode() == QLineEdit.EchoMode.Password
    dialog.api_key.setText(" test-key ")
    assert dialog.formality.isEnabled()

    try:
        dialog.test_button.click()
        qtbot.waitUntil(entered.is_set)
        assert not dialog.buttons.isEnabled()
        dialog.reject()
        assert dialog.isVisible()
    finally:
        release.set()
        dialog.wait_for_operation()

    qtbot.waitUntil(dialog.buttons.isEnabled)
    assert dialog.key_status.text() == message
    dialog.api_key.setText("different-key")
    assert not dialog.key_status.text()
    dialog.reject()
    save.assert_not_called()


def test_override_and_invalid_values(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DLX_CHUNK_SIZE", "1500")
    settings = Settings.load(tmp_path / "settings.json", env_file=None)
    save = Mock(return_value=None)
    check = Mock()
    dialog = SettingsDialog(
        settings, "override-key", key_overridden=True, save=save, check_key=check
    )
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.chunk_size.value() == 1500
    assert not dialog.chunk_size.isEnabled()
    assert dialog.api_key.isReadOnly()
    assert not dialog.api_key.text()
    try:
        dialog.test_button.click()
        qtbot.waitUntil(dialog.buttons.isEnabled)
    finally:
        dialog.wait_for_operation()
    check.assert_called_once_with("override-key")

    save_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
    dialog.retries.clear()
    save_button.click()
    save.assert_not_called()
    assert dialog.save_status.text()
    dialog.retries.setText("0")
    save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    updated, key = save.call_args.args
    assert updated.chunk_size == 1500
    assert updated.retries == 0
    assert updated.is_overridden("chunk_size")
    assert key == "override-key"


def test_disabling_history_discards_an_incomplete_limit(qtbot: QtBot) -> None:
    save = Mock(return_value=None)
    dialog = SettingsDialog(
        Settings(history_limit=125), None, key_overridden=False, save=save, check_key=Mock()
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.history_limit.clear()
    dialog.history_enabled.setChecked(False)
    assert not dialog.history_limit.isEnabled()
    dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    settings, _ = save.call_args.args
    assert not settings.history_enabled
    assert settings.history_limit == 125
