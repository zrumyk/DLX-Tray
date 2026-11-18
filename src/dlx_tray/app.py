"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

import logging
from contextlib import ExitStack

from pydantic_settings import SettingsError
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from dlx_tray import APP_NAME, __version__
from dlx_tray.config import Settings
from dlx_tray.logging_setup import file_logging
from dlx_tray.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)

    try:
        settings = Settings.load()
    except (OSError, ValueError, SettingsError):
        QMessageBox.critical(
            None,
            APP_NAME,
            QCoreApplication.translate(
                "Startup",
                "Could not load settings. Check settings.json and the DLX environment overrides.",
            ),
        )
        return 1

    with ExitStack() as stack:
        try:
            stack.enter_context(file_logging(settings.log_level))
        except OSError:
            QMessageBox.warning(
                None,
                APP_NAME,
                QCoreApplication.translate("Startup", "Could not open the application log."),
            )
        log.info("Starting DLX Tray %s", __version__)
        window = MainWindow()
        window.show()
        return app.exec()
