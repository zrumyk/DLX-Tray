"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

import logging
from contextlib import ExitStack

from pydantic_settings import SettingsError
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from dlx_tray import APP_NAME, __version__
from dlx_tray.config import Settings
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.logging_setup import file_logging
from dlx_tray.providers.deeplx import DeeplxTranslator
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
        job = None
        if settings.deeplx_url is not None:
            token = settings.deeplx_token.get_secret_value() if settings.deeplx_token else ""
            translator = stack.enter_context(DeeplxTranslator(str(settings.deeplx_url), token))
            job = TranslationJob(
                translator=translator,
                max_units=settings.chunk_size,
                request_delay=settings.request_delay,
                retry_policy=RetryPolicy(max_attempts=settings.max_attempts),
            )
        window = MainWindow(
            source=settings.source_lang,
            target=settings.target_lang,
            chunk_size=settings.chunk_size,
            translation_job=job,
            formality=settings.formality,
        )
        if job is None:
            window.statusBar().showMessage(
                QCoreApplication.translate("Startup", "Set DLX_DEEPLX_URL to translate.")
            )
        window.show()
        try:
            return app.exec()
        finally:
            window.stop_translation()
