"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

import logging
from contextlib import ExitStack

from pydantic_settings import SettingsError
from PySide6.QtCore import QCoreApplication, Qt, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from dlx_tray import APP_NAME, __version__
from dlx_tray.config import Settings
from dlx_tray.core.models import ProviderUnavailableError
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.logging_setup import file_logging
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.ui.main_window import MainWindow

log = logging.getLogger(__name__)


class _DLXStartup(QThread):
    def __init__(self, process: DLXProcess) -> None:
        super().__init__()
        self.process = process
        self.ready = False

    def run(self) -> None:
        try:
            self.process.start()
        except (ProviderUnavailableError, OSError) as exc:
            log.warning("DLX startup failed: %s", exc)
        else:
            self.ready = True


def _run_window(app: QApplication, settings: Settings, stack: ExitStack) -> int:
    window = MainWindow(
        source=settings.source_lang,
        target=settings.target_lang,
        chunk_size=settings.chunk_size,
        formality=settings.formality,
    )

    def connect_dlx(url: str, token: str, status: str) -> None:
        translator = stack.enter_context(DeeplxTranslator(url, token))
        job = TranslationJob(
            translator=translator,
            max_units=settings.chunk_size,
            request_delay=settings.request_delay,
            retry_policy=RetryPolicy(max_attempts=settings.max_attempts),
        )
        window.set_dlx_connection(status, job)

    startup = None
    if settings.deeplx_url is not None:
        token = settings.deeplx_token.get_secret_value() if settings.deeplx_token else ""
        connect_dlx(
            str(settings.deeplx_url),
            token,
            QCoreApplication.translate("Startup", "Using external DLX server"),
        )
    else:
        process = DLXProcess()
        stack.callback(process.stop)
        startup = _DLXStartup(process)
        window.set_dlx_connection(QCoreApplication.translate("Startup", "Starting DLX…"))

        def startup_finished() -> None:
            if not window.isVisible():
                return
            if startup.ready:
                connect_dlx(
                    process.base_url,
                    process.token,
                    QCoreApplication.translate("Startup", "DLX ready"),
                )
            else:
                window.set_dlx_connection(QCoreApplication.translate("Startup", "DLX unavailable"))

        connection = startup.finished.connect(startup_finished, Qt.ConnectionType.QueuedConnection)
        stack.callback(startup.disconnect, connection)

    try:
        window.show()
        if startup is not None:
            startup.start()
        return app.exec()
    finally:
        # The translator and process remain alive until both workers have stopped.
        window.stop_translation()
        if startup is not None:
            startup.wait()


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
        return _run_window(app, settings, stack)
