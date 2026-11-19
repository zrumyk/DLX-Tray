"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

import logging
from contextlib import ExitStack

from keyring.errors import KeyringError
from pydantic_settings import SettingsError
from PySide6.QtCore import QByteArray, QCoreApplication, QRect, Qt, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from dlx_tray import APP_NAME, __version__, config
from dlx_tray.config import Settings
from dlx_tray.core.models import ProviderUnavailableError
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.logging_setup import file_logging
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import FallbackTranslator
from dlx_tray.secrets import delete_deepl_api_key, get_deepl_api_key, set_deepl_api_key
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.ui.localization import interface_language
from dlx_tray.ui.main_window import MainWindow
from dlx_tray.ui.settings_dialog import SettingsDialog
from dlx_tray.ui.theme import apply_theme

log = logging.getLogger(__name__)

# Qt's geometry format v3 has used at least 66 bytes since Qt 5.12.
_MIN_WINDOW_GEOMETRY_BYTES = 66


def _window_placement(window: MainWindow) -> tuple[QRect, bool, bool]:
    return window.normalGeometry(), window.isMaximized(), window.isFullScreen()


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


def _check_deepl_api_key(key: str) -> None:
    official = DeepLOfficialTranslator(key, timeout=5.0)
    try:
        official.check_key()
    finally:
        official.close()


def _run_window(app: QApplication, settings: Settings, stack: ExitStack) -> int:
    apply_theme(app, settings.theme)
    window = MainWindow(
        source=settings.source_lang,
        target=settings.target_lang,
        chunk_size=settings.chunk_size,
        formality=settings.formality,
    )
    history_path = config.app_data_dir() / "history.sqlite3"
    window.set_history(history_path, enabled=settings.history_enabled, limit=settings.history_limit)
    geometry_restored = False
    if settings.window_geometry:
        try:
            geometry = bytes.fromhex(settings.window_geometry)
        except ValueError:
            log.warning("Invalid saved window geometry encoding")
        else:
            if len(geometry) >= _MIN_WINDOW_GEOMETRY_BYTES:
                geometry_restored = window.restoreGeometry(QByteArray(geometry))
            if not geometry_restored:
                log.warning("Qt could not restore the saved window geometry")

    override = settings.deepl_api_key
    override_key = (override.get_secret_value().strip() or None) if override else None
    api_key = get_deepl_api_key(override_key)
    official = DeepLOfficialTranslator(api_key) if api_key else None
    dlx: DeeplxTranslator | None = None
    translator: Translator | None = None
    status = QCoreApplication.translate("Startup", "Starting DLX…")
    starting = settings.deeplx_url is None

    def close_official() -> None:
        if official is not None:
            official.close()

    stack.callback(close_official)

    def refresh_job() -> None:
        job = (
            TranslationJob(
                translator=translator,
                max_units=settings.chunk_size,
                request_delay=settings.request_delay,
                retry_policy=RetryPolicy(max_attempts=settings.max_attempts),
            )
            if translator is not None
            else None
        )
        window.set_dlx_connection(
            status, job, chunk_size=settings.chunk_size, formality=settings.formality
        )

    def refresh_provider() -> None:
        nonlocal translator
        if starting:
            translator = None
        elif dlx is not None and official is not None:
            translator = FallbackTranslator(dlx, official)
        else:
            translator = dlx or official

    def save_preferences(updated: Settings, key: str | None) -> str | None:
        nonlocal settings, official, api_key
        try:
            updated.save(changes_from=settings)
        except (OSError, ValueError) as exc:
            log.warning("Could not save settings (%s)", type(exc).__name__)
            return QCoreApplication.translate("SettingsDialog", "Could not save settings.")

        if updated.theme != settings.theme:
            apply_theme(app, updated.theme)
        settings = updated
        window.set_history(
            history_path, enabled=settings.history_enabled, limit=settings.history_limit
        )
        error = None
        if override_key is None and key != api_key:
            try:
                if key is None:
                    delete_deepl_api_key()
                else:
                    set_deepl_api_key(key)
            except KeyringError:
                log.warning("Could not update the stored DeepL API key")
                error = QCoreApplication.translate(
                    "SettingsDialog", "Preferences saved. Could not update the stored API key."
                )
            else:
                close_official()
                api_key = key
                official = DeepLOfficialTranslator(key) if key is not None else None
                if isinstance(translator, FallbackTranslator) and official is not None:
                    translator.replace_secondary(official)
                else:
                    refresh_provider()
        refresh_job()
        return error

    def open_settings() -> None:
        dialog = SettingsDialog(
            settings,
            api_key,
            key_overridden=override_key is not None,
            save=save_preferences,
            check_key=_check_deepl_api_key,
            parent=window,
        )
        try:
            dialog.exec()
        finally:
            dialog.wait_for_key_check()
            dialog.deleteLater()

    window.settings_requested.connect(open_settings)

    startup = None
    if settings.deeplx_url is not None:
        token = settings.deeplx_token.get_secret_value() if settings.deeplx_token else ""
        dlx = stack.enter_context(DeeplxTranslator(str(settings.deeplx_url), token))
        status = QCoreApplication.translate("Startup", "Using external DLX server")
        refresh_provider()
    else:
        process = DLXProcess()
        stack.callback(process.stop)
        startup = _DLXStartup(process)

        def startup_finished() -> None:
            nonlocal dlx, status, starting
            if not window.isVisible():
                return
            starting = False
            if startup.ready:
                dlx = stack.enter_context(DeeplxTranslator(process.base_url, process.token))
                status = QCoreApplication.translate("Startup", "DLX ready")
            else:
                status = QCoreApplication.translate("Startup", "DLX unavailable")
            refresh_provider()
            refresh_job()

        connection = startup.finished.connect(startup_finished, Qt.ConnectionType.QueuedConnection)
        stack.callback(startup.disconnect, connection)

    refresh_job()
    try:
        window.show()
        initial_placement = _window_placement(window)
        if startup is not None:
            startup.start()
        result = app.exec()
    finally:
        # The translator and process remain alive until both workers have stopped.
        window.stop_translation()
        if startup is not None:
            startup.wait()

    updated = settings.model_copy()
    updated.source_lang, updated.target_lang = window.selected_languages
    if not geometry_restored or _window_placement(window) != initial_placement:
        updated.window_geometry = window.saveGeometry().data().hex()
    if updated != settings:
        try:
            updated.save(changes_from=settings)
        except (OSError, ValueError) as exc:
            log.warning("Could not save window preferences (%s)", type(exc).__name__)
    return result


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
        stack.enter_context(interface_language(app, settings.ui_language))
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
