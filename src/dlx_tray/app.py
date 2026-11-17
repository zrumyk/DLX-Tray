"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from dlx_tray import APP_NAME, __version__
from dlx_tray.ui.main_window import MainWindow


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)

    window = MainWindow()
    window.show()
    return app.exec()
