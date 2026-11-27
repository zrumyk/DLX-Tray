"""Application bootstrap: creates QApplication, wires services, shows the main window."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from pathlib import Path

from keyring.errors import KeyringError
from pydantic_settings import SettingsError
from PySide6.QtCore import QByteArray, QCoreApplication, QRect, Qt, QThread
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication, QMessageBox

from dlx_tray import APP_NAME, __version__, config
from dlx_tray.config import Settings
from dlx_tray.core.diagnostics import DiagnosticTimeline, DLXConnection
from dlx_tray.core.models import ProviderUnavailableError
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy, event_sleep
from dlx_tray.core.single_flight import SingleFlight
from dlx_tray.logging_setup import file_logging
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import FallbackTranslator
from dlx_tray.secrets import delete_deepl_api_key, get_deepl_api_key, set_deepl_api_key
from dlx_tray.services import autostart
from dlx_tray.services.cache import TranslationCache
from dlx_tray.services.deeplx_process import DLXProcess, resolve_binary
from dlx_tray.services.diagnostics import markdown_report
from dlx_tray.services.history import HistoryStore
from dlx_tray.ui.localization import interface_language
from dlx_tray.ui.main_window import MainWindow
from dlx_tray.ui.selection_hotkey import SelectionHotkey
from dlx_tray.ui.selection_popup import SelectionPopup
from dlx_tray.ui.settings_dialog import SettingsDialog
from dlx_tray.ui.single_instance import single_instance
from dlx_tray.ui.theme import apply_theme
from dlx_tray.ui.workers import TranslationWorker

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


class _DiagnosticsExport(QThread):
    def __init__(self, collect: Callable[[], str]) -> None:
        super().__init__()
        self._collect = collect
        self.report: str | None = None

    def run(self) -> None:
        try:
            self.report = self._collect()
        except Exception as exc:
            log.warning("Could not collect diagnostics (%s)", type(exc).__name__)


def _check_deepl_api_key(key: str) -> None:
    official = DeepLOfficialTranslator(key, timeout=5.0)
    try:
        official.check_key()
    finally:
        official.close()


def _run_window(
    app: QApplication,
    settings: Settings,
    stack: ExitStack,
    server: QLocalServer | None = None,
    *,
    start_in_tray: bool = False,
) -> int:
    apply_theme(app, settings.theme)
    window = MainWindow(
        source=settings.source_lang,
        target=settings.target_lang,
        chunk_size=settings.chunk_size,
        formality=settings.formality,
        ui_language=settings.ui_language,
    )
    history_path = config.app_data_dir() / "history.sqlite3"
    cache = TranslationCache(history_path.with_name("cache.db"))
    flights = SingleFlight()
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

    diagnostics = DiagnosticTimeline()
    export: _DiagnosticsExport | None = None
    override = settings.deepl_api_key
    override_key = (override.get_secret_value().strip() or None) if override else None
    api_key = get_deepl_api_key(override_key)
    official = DeepLOfficialTranslator(api_key, diagnostics=diagnostics) if api_key else None
    dlx: DeeplxTranslator | None = None
    translator: Translator | None = None
    job: TranslationJob | None = None
    provider_status = ""
    dlx_status = QCoreApplication.translate("Startup", "Starting DLX…")
    dlx_connection: DLXConnection | None = None
    startup: _DLXStartup | None = None

    def cooldown_deadline() -> float:
        return (
            dlx.cooldown_deadline
            if dlx is not None and settings.provider_mode == "automatic"
            else 0.0
        )

    tray_available = window.enable_tray(app.windowIcon(), cooldown_deadline)
    hotkey = SelectionHotkey(window)
    popup: SelectionPopup | None = None

    def configure_hotkey() -> None:
        if app.platformName() == "windows" and not hotkey.configure(settings.selection_hotkey):
            window.statusBar().showMessage(
                QCoreApplication.translate("Startup", "Could not enable the selection hotkey.")
            )

    def popup_closed() -> None:
        nonlocal popup
        popup = None

    def translate_selection(text: str) -> None:
        nonlocal popup
        if popup is not None or window.is_closed:
            return
        history = partial(HistoryStore, history_path, settings.history_limit)
        worker = (
            TranslationWorker(
                job,
                text,
                *window.selected_languages,
                settings.formality,
                history if settings.history_enabled else None,
            )
            if job is not None
            else None
        )
        popup = SelectionPopup(worker, provider_status, window)
        popup.finished.connect(popup_closed)
        if screen := app.screenAt(QCursor.pos()):
            popup.move(screen.availableGeometry().center() - popup.rect().center())
        popup.show()
        popup.raise_()
        popup.activateWindow()

    hotkey.selection_ready.connect(translate_selection)
    window.closed.connect(hotkey.stop)

    def activate_window() -> None:
        assert server is not None
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.abort()
            socket.deleteLater()
        window.restore_from_tray()

    if server is not None:
        server.newConnection.connect(activate_window)
        window.closed.connect(server.close)

    def copy_diagnostics() -> None:
        nonlocal export
        if export is not None:
            return
        if getattr(sys, "frozen", False):
            lock_path = Path(__file__).with_name("resources") / "deeplx.lock.json"
        else:
            lock_path = Path(__file__).resolve().parents[2] / "scripts" / "deeplx.lock.json"
        collect = partial(
            markdown_report,
            binary=resolve_binary(),
            dlx_lock=lock_path,
            log_path=config.app_data_dir() / "logs" / "dlx-tray.log",
            provider=window.diagnostic_provider,
            dlx_status=(dlx_connection or "starting")
            if settings.provider_mode == "automatic"
            else "disabled",
            rate_limited=window.diagnostic_rate_limited,
            has_key=bool(api_key),
            chunk_size=settings.chunk_size,
            request_delay=settings.request_delay,
            concurrency=job.effective_concurrency if job is not None else None,
            retries=settings.retries,
            timeline=diagnostics.snapshot(),
        )

        def finish_export() -> None:
            nonlocal export
            assert export is not None
            if export.report is None:
                message = QCoreApplication.translate("Diagnostics", "Could not copy diagnostics.")
            else:
                app.clipboard().setText(export.report)
                message = QCoreApplication.translate("Diagnostics", "Diagnostics copied.")
            window.diagnostics_finished.emit(message)
            export.deleteLater()
            export = None

        export = _DiagnosticsExport(collect)
        export.finished.connect(finish_export, Qt.ConnectionType.QueuedConnection)
        export.start()

    window.diagnostics_requested.connect(copy_diagnostics)

    def close_official() -> None:
        if official is not None:
            official.close()

    stack.callback(close_official)

    def refresh_job() -> None:
        nonlocal job, provider_status
        refresh_provider()
        requires_settings = settings.provider_mode == "official_only" and official is None
        if requires_settings:
            status = QCoreApplication.translate("Startup", "Add a DeepL API key in Settings")
        elif settings.provider_mode == "official_only":
            status = QCoreApplication.translate("Startup", "Using the official DeepL API")
        else:
            status = dlx_status
        provider_status = status
        job = (
            TranslationJob(
                translator=translator,
                max_units=settings.chunk_size,
                request_delay=settings.request_delay,
                concurrency=settings.dlx_concurrency,
                retry_policy=RetryPolicy(max_attempts=settings.max_attempts),
                sleep=event_sleep,
                cache=cache if settings.cache_enabled else None,
                flights=flights,
                diagnostics=diagnostics,
            )
            if translator is not None
            else None
        )
        window.set_dlx_connection(
            status,
            job,
            chunk_size=settings.chunk_size,
            formality=settings.formality,
            requires_settings=requires_settings,
            can_add_api_key=translator is dlx and dlx is not None and api_key is None,
        )

    def refresh_provider() -> None:
        nonlocal translator
        if settings.provider_mode == "official_only":
            translator = official
        elif dlx_connection == "starting":
            translator = None
        elif dlx is not None and official is not None:
            if not isinstance(translator, FallbackTranslator):
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
        shortcut_changed = updated.selection_hotkey != settings.selection_hotkey
        settings = updated
        if shortcut_changed:
            configure_hotkey()
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
                official = (
                    DeepLOfficialTranslator(key, diagnostics=diagnostics)
                    if key is not None
                    else None
                )
                if isinstance(translator, FallbackTranslator) and official is not None:
                    translator.replace_secondary(official)
        start_dlx_if_needed()
        refresh_job()
        return error

    def open_settings() -> None:
        autostart_supported = autostart.is_supported()
        registered = None
        if autostart_supported:
            try:
                registered = autostart.is_registered()
            except OSError as exc:
                log.warning("Could not read Windows startup settings (%s)", type(exc).__name__)
        dialog = SettingsDialog(
            settings,
            api_key,
            key_overridden=override_key is not None,
            save=save_preferences,
            check_key=_check_deepl_api_key,
            clear_cache=cache.clear,
            autostart_registered=registered,
            set_autostart=autostart.set_registered if autostart_supported else None,
            parent=window,
        )
        if api_key is None:
            dialog.api_key.setFocus()
        try:
            dialog.exec()
        finally:
            dialog.wait_for_operation()
            dialog.deleteLater()

    window.settings_requested.connect(open_settings)

    def start_dlx_if_needed() -> None:
        nonlocal dlx, dlx_connection, dlx_status, startup
        if settings.provider_mode == "official_only" or dlx_connection is not None:
            return
        if settings.deeplx_url is not None:
            token = settings.deeplx_token.get_secret_value() if settings.deeplx_token else ""
            dlx = stack.enter_context(
                DeeplxTranslator(str(settings.deeplx_url), token, diagnostics=diagnostics)
            )
            dlx_status = QCoreApplication.translate("Startup", "Using external DLX server")
            dlx_connection = "external"
            return

        process = DLXProcess()
        stack.callback(process.stop)
        worker = _DLXStartup(process)
        startup = worker
        dlx_connection = "starting"

        def startup_finished() -> None:
            nonlocal dlx, dlx_connection, dlx_status
            if window.is_closed:
                return
            if worker.ready:
                dlx = stack.enter_context(
                    DeeplxTranslator(process.base_url, process.token, diagnostics=diagnostics)
                )
                dlx_status = QCoreApplication.translate("Startup", "DLX ready")
                dlx_connection = "ready"
            else:
                dlx_status = QCoreApplication.translate("Startup", "DLX unavailable")
                dlx_connection = "unavailable"
            if settings.provider_mode == "automatic":
                refresh_job()

        connection = worker.finished.connect(startup_finished, Qt.ConnectionType.QueuedConnection)
        stack.callback(worker.disconnect, connection)
        worker.start()

    try:
        start_dlx_if_needed()
        refresh_job()
        if not start_in_tray or not tray_available:
            window.show()
        configure_hotkey()
        initial_placement = _window_placement(window)
        if server is not None and server.hasPendingConnections():
            activate_window()
        result = app.exec()
    finally:
        hotkey.stop()
        if popup is not None:
            popup.stop_translation()
        window.hide_tray()
        if server is not None:
            server.close()
            server.newConnection.disconnect(activate_window)
        # Keep the providers alive until their workers have stopped.
        window.stop_translation()
        if startup is not None:
            startup.wait()
        if export is not None:
            export.wait()

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
    start_in_tray = autostart.ARGUMENT in argv[1:]
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("resources") / "dlx-tray.ico")))

    with ExitStack() as stack:
        try:
            server = stack.enter_context(
                single_instance(config.app_data_dir(), activate=not start_in_tray)
            )
        except OSError as exc:
            log.warning("Could not initialize single-instance control: %s", type(exc).__name__)
            try:
                language = Settings.load().ui_language
            except (OSError, ValueError, SettingsError):
                language = config.system_ui_language()
            with interface_language(app, language):
                QMessageBox.critical(
                    None,
                    APP_NAME,
                    QCoreApplication.translate(
                        "Startup",
                        "Could not open DLX Tray. Check whether it is already running.",
                    ),
                )
            return 1
        if server is None:
            return 0
        return _run_primary(app, stack, server, start_in_tray=start_in_tray)


def _run_primary(
    app: QApplication, stack: ExitStack, server: QLocalServer, *, start_in_tray: bool
) -> int:
    try:
        settings = Settings.load(migrate=True)
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

    log_opened = True
    try:
        stack.enter_context(file_logging(settings.log_level))
    except OSError:
        log_opened = False
    stack.enter_context(interface_language(app, settings.ui_language))
    if not log_opened:
        QMessageBox.warning(
            None,
            APP_NAME,
            QCoreApplication.translate("Startup", "Could not open the application log."),
        )
    log.info("Starting DLX Tray %s", __version__)
    if settings.migration_error is not None:
        log.warning("Could not persist settings migration (%s)", settings.migration_error)
    return _run_window(app, settings, stack, server, start_in_tray=start_in_tray)
