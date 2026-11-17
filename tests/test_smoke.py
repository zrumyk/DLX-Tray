from pytestqt.qtbot import QtBot

from dlx_tray import APP_NAME
from dlx_tray.ui.main_window import MainWindow


def test_main_window_opens(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert window.windowTitle().startswith(APP_NAME)
