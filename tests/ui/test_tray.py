"""Exercise tray lifecycle, tooltip state, and profile activation."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
from pytestqt.qtbot import QtBot

from dlx_tray.ui import main_window
from dlx_tray.ui.main_window import MainWindow
from dlx_tray.ui.single_instance import single_instance


def test_launch_with_same_profile_contacts_primary(qtbot: QtBot, tmp_path: Path) -> None:
    with single_instance(tmp_path) as primary:
        assert primary is not None
        with qtbot.waitSignal(primary.newConnection), single_instance(tmp_path) as duplicate:
            assert duplicate is None
        assert primary.hasPendingConnections()
        with single_instance(tmp_path / "other") as other:
            assert other is not None
    with single_instance(tmp_path) as restarted:
        assert restarted is not None


@pytest.fixture
def tray_window(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    window = MainWindow()
    qtbot.addWidget(window)
    icon = QPixmap(16, 16)
    icon.fill(Qt.GlobalColor.blue)
    window.enable_tray(QIcon(icon), lambda: 110.0)
    return window


def test_minimize_close_and_restore_keep_window_state(
    tray_window: MainWindow, qtbot: QtBot
) -> None:
    window = tray_window
    window.showMaximized()
    window.source_editor.setPlainText("Keep this text")
    window.showMinimized()
    qtbot.waitUntil(lambda: not window.isVisible())
    tray = window.findChild(QSystemTrayIcon)
    assert tray is not None and tray.isVisible()
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.isVisible() and window.isMaximized()
    assert not window.close()
    assert not window.isVisible()
    menu = tray.contextMenu()
    assert menu is not None
    menu.actions()[0].trigger()
    assert window.isVisible() and window.isMaximized()
    assert window.source_editor.toPlainText() == "Keep this text"


def test_tooltip_reuses_provider_state_until_cooldown_expires(
    tray_window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = tray_window
    window.show()
    window.set_dlx_connection("Using external DLX server")
    window._show_provider("deepl")
    window.close()
    now = 100.0
    monkeypatch.setattr(main_window, "monotonic", lambda: now)
    window._tray_timer.timeout.emit()
    tray = window.findChild(QSystemTrayIcon)
    assert tray is not None
    assert "Provider: DeepL API" in tray.toolTip()
    assert "Using external DLX server" in tray.toolTip()
    assert "DLX cooldown: 10 s" in tray.toolTip()
    now = 111.0
    window._tray_timer.timeout.emit()
    assert "DLX cooldown" not in tray.toolTip()


def test_exit_from_hidden_tray_exits_event_loop(
    qapp: QApplication, tray_window: MainWindow
) -> None:
    window = tray_window
    window.show()
    window.close()
    tray = window.findChild(QSystemTrayIcon)
    assert tray is not None
    # Fail promptly if the hidden window closes without exiting QApplication.
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(lambda: qapp.exit(2))
    timeout.start(1000)
    QTimer.singleShot(0, window.exit_action.trigger)
    try:
        assert qapp.exec() == 0
        assert not tray.isVisible()
    finally:
        timeout.stop()


def test_declining_exit_keeps_unsaved_file(
    tray_window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = tray_window
    window.show()
    window._file_unsaved = True
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    window.close()
    assert not window.isVisible()
    window.exit_action.trigger()
    assert window.isVisible()
    assert window._file_unsaved
    assert not window.close()
    assert not window.isVisible()
