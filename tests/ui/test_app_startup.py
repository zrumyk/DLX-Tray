"""Exercise startup and shutdown through the application event loop."""

import json
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from itertools import pairwise
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import ANY, Mock, call

import pytest
import requests
from keyring.errors import KeyringError
from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory, running_fake_dlx

from dlx_tray import app, config, secrets
from dlx_tray.config import Settings, Theme
from dlx_tray.core.chunker import paragraphs
from dlx_tray.core.models import Formality, Language, ProviderUnavailableError, TranslationResult
from dlx_tray.core.retry import RequestDispatch
from dlx_tray.providers import deeplx, fallback
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.services.history import HistoryStore
from dlx_tray.ui.about_dialog import AboutDialog
from dlx_tray.ui.error_dialog import ErrorDialog
from dlx_tray.ui.history_dialog import HistoryDialog
from dlx_tray.ui.main_window import MainWindow
from dlx_tray.ui.settings_dialog import SettingsDialog


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Mock:
    vault = Mock()
    vault.get_password.return_value = None
    monkeypatch.setattr(secrets, "_credential_manager", lambda: nullcontext(vault))
    return vault


@pytest.fixture
def run_window(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, vault: Mock, tmp_path: Path
) -> Callable[[Settings, Callable[[MainWindow, QLabel], None]], None]:
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    for name in ("DLX_SOURCE_LANG", "DLX_TARGET_LANG", "DLX_WINDOW_GEOMETRY", "DLX_THEME"):
        monkeypatch.delenv(name, raising=False)

    def run(settings: Settings, check: Callable[[MainWindow, QLabel], None]) -> None:
        windows: list[MainWindow] = []

        class StartupWindow(MainWindow):
            def show(self) -> None:
                windows.append(self)
                qtbot.addWidget(self)
                super().show()

        monkeypatch.setattr(app, "MainWindow", StartupWindow)

        def check_and_close() -> None:
            window = windows[0]
            try:
                status = window.findChild(QLabel, "dlx_status")
                assert status is not None
                assert status.isVisible()
                check(window, status)
            finally:
                if window.isVisible():
                    window.close()

        QTimer.singleShot(0, check_and_close)
        with ExitStack() as stack:
            assert app._run_window(qapp, settings, stack) == 0

    return run


def test_startup_loads_the_window_icon(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(Settings, "load", lambda *, migrate: Settings())
    monkeypatch.setattr(app, "file_logging", lambda _level: nullcontext())

    def check_icon(application: QApplication, _settings: Settings, _stack: ExitStack) -> int:
        icon = application.windowIcon()
        assert QSize(16, 16) in icon.availableSizes()
        assert QSize(256, 256) in icon.availableSizes()
        assert not icon.pixmap(16, 16).isNull()
        return 0

    monkeypatch.setattr(app, "_run_window", check_icon)
    previous_icon = qapp.windowIcon()
    try:
        assert app.run([]) == 0
    finally:
        qapp.setWindowIcon(previous_icon)


def test_startup_migrates_request_delay_before_opening_the_window(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DLX_REQUEST_DELAY", raising=False)
    path = tmp_path / "settings.json"
    path.write_text('{"request_delay": 2.5, "ui_language": "en"}', encoding="utf-8")
    observed_delays: list[float] = []

    def check_settings(_app: QApplication, settings: Settings, _stack: ExitStack) -> int:
        observed_delays.append(settings.request_delay)
        assert json.loads(path.read_text(encoding="utf-8"))["settings_version"] == 1
        if len(observed_delays) == 1:
            settings.model_copy(update={"request_delay": 2.5}).save(changes_from=settings)
        return 0

    monkeypatch.setattr(app, "_run_window", check_settings)
    for _ in range(2):
        assert app.run([]) == 0
    assert observed_delays == [0, 2.5]


def test_copy_diagnostics_redacts_secrets_and_keeps_error_dialog_open(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_marker = "DIAG_SOURCE_MARKER"
    translated_marker = "DIAG_TRANSLATION_MARKER"
    key_marker = "DIAG_KEY_MARKER:fx"
    token_marker = "DIAG_TOKEN_MARKER"
    query_marker = "DIAG_QUERY_MARKER"
    markers = (source_marker, translated_marker, key_marker, token_marker, query_marker)
    success = requests.Response()
    success.status_code = 200
    success._content = json.dumps(
        {"translations": [{"text": translated_marker, "detected_source_language": "en"}]}
    ).encode()
    failure = requests.Response()
    failure.status_code = 403
    failure._content = json.dumps({"message": " ".join(markers)}).encode()
    send = Mock(side_effect=[success, failure])
    monkeypatch.setattr(requests.Session, "send", send)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "dlx-tray.log"
    log_prefix = "2026-11-27 10:00:00,000 INFO dlx_tray.app "
    log_path.write_text(
        "".join(f"{log_prefix}DLX stopped (pid {pid})\n" for pid in range(101))
        + f"{log_prefix}unexpected payload: {' '.join(markers)}\n",
        encoding="utf-8",
    )

    clipboard = QApplication.clipboard()
    previous_clipboard = clipboard.text()

    def copy(dialog: AboutDialog | ErrorDialog) -> str:
        clipboard.clear()
        dialog.diagnostics_button.click()
        qtbot.waitUntil(lambda: clipboard.text().startswith("## DLX Tray diagnostics"))
        assert QApplication.activeModalWidget() is dialog
        assert dialog.diagnostics_feedback.text() == "Diagnostics copied."
        report = clipboard.text()
        for marker in markers:
            assert marker not in report
        assert "DLX connection: external" in report
        assert "DeepL API key: set" in report
        log_lines = [line for line in report.splitlines() if line.startswith("    ")]
        assert len(log_lines) == 100
        assert log_lines[0].endswith("DLX stopped (pid 2)")
        assert log_lines[-1].endswith("[redacted]")
        return report

    def check(window: MainWindow, _status: QLabel) -> None:
        source_text = f"{source_marker} {'a' * 170}\n\n{source_marker} {'b' * 170}"
        window.source_editor.setPlainText(source_text)
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert translated_marker in window.result_editor.toPlainText()

        def check_about() -> None:
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, AboutDialog)
            try:
                report = copy(dialog)
                assert "Active provider: DeepL API" in report
                assert "HTTP 200, Retry-After unknown, cache unknown" in report
                assert "HTTP 429, Retry-After 9 s, cache unknown" in report
                assert "Tray wait:" in report
            finally:
                dialog.reject()

        QTimer.singleShot(0, check_about)
        window.about_action.trigger()

        window.source_editor.setPlainText(source_marker)
        window.translate_button.click()
        qtbot.waitUntil(lambda: isinstance(QApplication.activeModalWidget(), ErrorDialog))
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ErrorDialog)
        assert dialog.heading.text() == "Translation failed in paragraph 1 of 1."
        assert "HTTP 403" in copy(dialog)
        dialog.cancel_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert send.call_count == 2

    try:
        with running_fake_dlx(["ok", "429-retry-after"], token=token_marker) as server:
            monkeypatch.setenv("DLX_DEEPLX_URL", f"{server.url}?trace={query_marker}")
            monkeypatch.setenv("DLX_DEEPLX_TOKEN", server.token)
            monkeypatch.setenv("DLX_DEEPL_API_KEY", key_marker)
            monkeypatch.setenv("DLX_CHUNK_SIZE", "200")
            monkeypatch.setenv("DLX_REQUEST_DELAY", "0.2")
            monkeypatch.setenv("DLX_RETRIES", "0")
            run_window(Settings.load(env_file=None), check)
    finally:
        clipboard.setText(previous_clipboard)


@pytest.mark.parametrize("maximized", [False, True], ids=["normal", "maximized"])
def test_window_geometry_and_languages_survive_restart(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
    fixture_text: Callable[[str], str],
    maximized: bool,
) -> None:
    expected = QRect(60, 65, 700, 400)

    def change_window(window: MainWindow, _status: QLabel) -> None:
        assert window.selected_languages == (Language.AUTO, Language.UK)
        window.setGeometry(expected)
        if maximized:
            window.showMaximized()
        window.source_languages.setCurrentIndex(window.source_languages.findData(Language.DE.value))
        window.target_languages.setCurrentIndex(
            window.target_languages.findData(Language.EN_GB.value)
        )
        window.source_editor.setPlainText(fixture_text("en_article.txt"))

    def check_restored(window: MainWindow, _status: QLabel) -> None:
        assert window.selected_languages == (Language.DE, Language.EN_GB)
        assert window.isMaximized() == maximized
        if maximized:
            window.showNormal()
        assert window.geometry() == expected
        assert window.source_editor.document().isEmpty()
        assert window.result_editor.document().isEmpty()

    with fake_dlx([]) as server:
        monkeypatch.setenv("DLX_DEEPLX_URL", server.url)
        run_window(Settings.load(env_file=None), change_window)
        run_window(Settings.load(env_file=None), check_restored)


def test_window_save_preserves_overrides_and_concurrent_settings(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
) -> None:
    initial = Settings(source_lang=Language.DE)
    initial.save()

    def change_window(window: MainWindow, _status: QLabel) -> None:
        assert window.selected_languages == (Language.FR, Language.UK)
        window.source_languages.setCurrentIndex(window.source_languages.findData(Language.ES.value))
        window.target_languages.setCurrentIndex(
            window.target_languages.findData(Language.EN_GB.value)
        )
        concurrent = Settings.load(env_file=None)
        concurrent.theme = "dark"
        concurrent.save(changes_from=initial)

    with fake_dlx([]) as server:
        monkeypatch.setenv("DLX_DEEPLX_URL", server.url)
        monkeypatch.setenv("DLX_SOURCE_LANG", "FR")
        run_window(Settings.load(env_file=None), change_window)

    monkeypatch.delenv("DLX_SOURCE_LANG")
    restored = Settings.load(env_file=None)
    assert restored.source_lang is Language.DE
    assert restored.target_lang is Language.EN_GB
    assert restored.theme == "dark"


def test_untouched_restored_window_keeps_concurrent_geometry(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
) -> None:
    other_window = MainWindow()
    qtbot.addWidget(other_window)
    other_window.setGeometry(100_000, 100_000, 700, 400)
    other_window.setWindowState(Qt.WindowState.WindowMaximized)
    initial = Settings(window_geometry=other_window.saveGeometry().data().hex())
    initial.save()
    other_window.setWindowState(Qt.WindowState.WindowNoState)
    other_window.setGeometry(40, 50, 710, 420)
    concurrent_geometry = other_window.saveGeometry().data().hex()

    def change_languages(window: MainWindow, _status: QLabel) -> None:
        assert window.isMaximized()
        assert window.screen().availableGeometry().intersects(window.frameGeometry())
        Settings(window_geometry=concurrent_geometry).save(changes_from=initial)
        window.target_languages.setCurrentIndex(window.target_languages.findData(Language.DE.value))

    with fake_dlx([]) as server:
        monkeypatch.setenv("DLX_DEEPLX_URL", server.url)
        run_window(Settings.load(env_file=None), change_languages)

    restored = Settings.load(env_file=None)
    assert restored.window_geometry == concurrent_geometry
    assert restored.target_lang is Language.DE


@pytest.mark.parametrize("geometry", ["not hex", "01d9d0cb", "00" * 66, 42])
def test_invalid_window_geometry_is_replaced(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
    tmp_path: Path,
    geometry: object,
) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"window_geometry": geometry, "source_lang": "DE"}), encoding="utf-8"
    )
    saved_geometry: str | None = None

    def check(window: MainWindow, _status: QLabel) -> None:
        nonlocal saved_geometry
        assert not window.isMaximized()
        assert window.selected_languages == (Language.DE, Language.UK)
        saved_geometry = window.saveGeometry().data().hex()

    with fake_dlx([]) as server:
        monkeypatch.setenv("DLX_DEEPLX_URL", server.url)
        run_window(Settings.load(env_file=None), check)

    assert saved_geometry is not None
    assert Settings.load(env_file=None).window_geometry == saved_geometry


@pytest.mark.parametrize("fails", [False, True], ids=["ready", "unavailable"])
def test_bundled_dlx_starts_in_background(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
    fails: bool,
) -> None:
    may_finish = Event()

    def start() -> None:
        assert may_finish.wait(timeout=5)
        if fails:
            raise ProviderUnavailableError("test startup failure")

    with fake_dlx([]) as server:
        process = Mock(spec=DLXProcess, base_url=server.url, token=server.token)
        process.start.side_effect = start
        monkeypatch.setattr(app, "DLXProcess", Mock(return_value=process))

        def check(window: MainWindow, status: QLabel) -> None:
            shortcut = window.findChild(QShortcut)
            try:
                assert status.text() == "Starting DLX…"
                window.source_editor.setPlainText("Hello")
                assert not window.translate_button.isEnabled()
                assert shortcut is not None
                assert not shortcut.isEnabled()
            finally:
                may_finish.set()

            if fails:
                qtbot.waitUntil(lambda: status.text() == "DLX unavailable")
                assert not window.translate_button.isEnabled()
                assert not shortcut.isEnabled()
            else:
                qtbot.waitUntil(window.translate_button.isEnabled)
                assert status.text() == "DLX ready"
                assert shortcut.isEnabled()
                window.translate_button.click()
                qtbot.waitUntil(window.translate_button.isEnabled)
                assert window.result_editor.toPlainText() == "[UK] Hello"
                assert status.text() == "DLX ready"
                assert status.isVisible()

        run_window(Settings(), check)
        process.start.assert_called_once_with()
        process.stop.assert_called_once_with()


def test_external_dlx_skips_bundled_process(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
) -> None:
    process_factory = Mock()
    monkeypatch.setattr(app, "DLXProcess", process_factory)

    def check(window: MainWindow, status: QLabel) -> None:
        assert status.text() == "Using external DLX server"
        window.source_editor.setPlainText("Hello")
        window.translate_button.click()
        qtbot.waitUntil(lambda: window.result_editor.toPlainText() == "[UK] Hello")
        qtbot.waitUntil(window.translate_button.isEnabled)

    with fake_dlx([]) as server:
        settings = Settings.model_validate({"deeplx_url": server.url, "deeplx_token": server.token})
        run_window(settings, check)
    process_factory.assert_not_called()


@pytest.mark.parametrize("startup_fails", [False, True], ids=["runtime-429", "startup-failure"])
@pytest.mark.parametrize("key_source", ["environment", "dotenv", "vault"])
def test_uses_official_api_when_dlx_is_unavailable(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
    startup_fails: bool,
    tmp_path: Path,
    key_source: str,
    vault: Mock,
) -> None:
    official = Mock(spec=app.DeepLOfficialTranslator)
    official.name = "deepl"

    def translate(*_args: object, dispatch: RequestDispatch) -> list[TranslationResult]:
        dispatch.on_start()
        return [TranslationResult("Привіт", Language.EN, "deepl")]

    official.translate.side_effect = translate
    official_factory = Mock(return_value=official)
    monkeypatch.setattr(app, "DeepLOfficialTranslator", official_factory)
    monkeypatch.delenv("DLX_DEEPL_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    vault.get_password.return_value = "stored-key:fx"
    if key_source != "vault":
        env_file.write_text("DLX_DEEPL_API_KEY=dotenv-key:fx\n", encoding="utf-8")
    if key_source == "environment":
        monkeypatch.setenv("DLX_DEEPL_API_KEY", "env-key:fx")

    def check(window: MainWindow, status: QLabel) -> None:
        window.source_editor.setPlainText("Hello")
        qtbot.waitUntil(window.translate_button.isEnabled)
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "Привіт"
        assert status.text() == (
            "DLX unavailable" if startup_fails else "Using external DLX server"
        )
        provider = window.findChild(QLabel, "provider_status")
        warning = window.findChild(QLabel, "rate_limit_status")
        assert provider is not None and provider.isVisible()
        assert provider.text() == "Provider: DeepL API"
        assert warning is not None and warning.isVisible() == (not startup_fails)
        if not startup_fails:
            assert warning.text() == "DLX rate limit reached."
        official.close.assert_not_called()

    with fake_dlx(["429"]) as server:
        if startup_fails:
            process = Mock(spec=DLXProcess)
            process.start.side_effect = ProviderUnavailableError("test startup failure")
            monkeypatch.setattr(app, "DLXProcess", Mock(return_value=process))
            monkeypatch.delenv("DLX_DEEPLX_URL", raising=False)
        else:
            monkeypatch.setenv("DLX_DEEPLX_URL", server.url)
            monkeypatch.setenv("DLX_DEEPLX_TOKEN", server.token)
        settings = Settings.load(tmp_path / "settings.json", env_file=env_file)
        run_window(settings, check)

    if key_source == "vault":
        vault.get_password.assert_called_once_with("DLX Tray", "deepl_api_key")
    else:
        vault.get_password.assert_not_called()
    expected_keys = {
        "environment": "env-key:fx",
        "dotenv": "dotenv-key:fx",
        "vault": "stored-key:fx",
    }
    official_factory.assert_called_once_with(expected_keys[key_source], diagnostics=ANY)
    official.translate.assert_called_once_with(
        ["Hello"], Language.AUTO, Language.UK, Formality.DEFAULT, dispatch=ANY
    )
    official.close.assert_called_once_with()


def test_close_during_dlx_startup_joins_worker_before_stopping_process(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = Event()
    startup_finished = Event()

    def start() -> None:
        assert closed.wait(timeout=5)
        startup_finished.set()

    def stop() -> None:
        assert startup_finished.is_set()

    process = Mock(spec=DLXProcess)
    process.start.side_effect = start
    process.stop.side_effect = stop
    monkeypatch.setattr(app, "DLXProcess", Mock(return_value=process))

    def check(window: MainWindow, status: QLabel) -> None:
        try:
            assert status.text() == "Starting DLX…"
            assert window.close()
        finally:
            closed.set()

    run_window(Settings(), check)
    process.stop.assert_called_once_with()


def _edit_settings(
    window: MainWindow, edit: Callable[[SettingsDialog], None], *, save: bool = True
) -> None:
    def interact() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, SettingsDialog)
        try:
            edit(dialog)
            if save:
                dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
                assert not dialog.isVisible()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, interact)
    window.settings_action.trigger()


def test_history_records_prunes_searches_and_reopens_translations(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    fixture_text: Callable[[str], str],
) -> None:
    source_file = tmp_path / "notes.txt"
    file_text = fixture_text("uk_article.txt")
    source_file.write_text(file_text, encoding="utf-8", newline="")
    path = tmp_path / "history.sqlite3"

    def check(window: MainWindow, _status: QLabel) -> None:
        def translate() -> None:
            assert window.translate_button.isEnabled()
            window.translate_button.click()
            qtbot.waitUntil(window.history_action.isEnabled)

        assert not path.exists()
        for text in ("Oldest message", "Вітаю зі Львова"):
            window.source_editor.setPlainText(text)
            translate()

        window._open_file(source_file)
        qtbot.waitUntil(window.translate_button.isEnabled)
        translate()
        saved = HistoryStore(path, 2).list()
        assert len(saved) == 2
        file_entry, text_entry = saved
        assert file_entry.file_name == source_file.name
        assert file_entry.source_text == file_text
        assert file_entry.target_text.strip() == window.result_editor.toPlainText().strip()
        assert text_entry.source_text == "Вітаю зі Львова"
        assert text_entry.target_text == "[EN] Вітаю зі Львова"
        assert text_entry.provider == "dlx"

        window._file_unsaved = False
        window.close_file_action.trigger()
        _edit_settings(window, lambda dialog: dialog.history_enabled.setChecked(False))
        window.target_languages.setCurrentIndex(window.target_languages.findData(Language.DE.value))
        window.source_editor.setPlainText("Do not record this")
        translate()
        assert HistoryStore(path, 2).list() == saved

        def browse() -> None:
            dialog = window.findChild(HistoryDialog)
            assert dialog is not None
            try:
                qtbot.waitUntil(lambda: dialog.entries.count() == 2)
                assert source_file.name in dialog.entries.item(0).text()
                dialog.entries.setCurrentRow(0)
                dialog.delete_button.click()
                qtbot.waitUntil(lambda: dialog.entries.count() == 1)
                dialog.search.setText("відсутній текст")
                qtbot.waitUntil(lambda: dialog.status.text() == "No matching translations.")
                dialog.search.setText(text_entry.source_text.upper())
                qtbot.waitUntil(lambda: dialog.entries.count() == 1)
                dialog.entries.setCurrentRow(0)
                dialog.open_button.click()
            finally:
                if dialog.isVisible():
                    dialog.reject()

        QTimer.singleShot(0, browse)
        window.history_action.trigger()
        assert window.selected_languages == (Language.UK, Language.EN)
        assert window.source_editor.toPlainText() == text_entry.source_text
        assert window.result_editor.toPlainText() == text_entry.target_text
        assert HistoryStore(path, 2).list() == [text_entry]

    with fake_dlx([]) as server:
        settings = Settings.model_validate(
            {
                "deeplx_url": server.url,
                "deeplx_token": server.token,
                "history_limit": 2,
                "source_lang": Language.UK,
                "target_lang": Language.EN,
                "request_delay": 0,
            }
        )
        run_window(settings, check)


def test_theme_applies_on_startup_and_after_saving(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
) -> None:
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    hints = qapp.styleHints()
    set_scheme = Mock()
    monkeypatch.setattr(hints, "setColorScheme", set_scheme)
    path = tmp_path / "settings.json"

    def select_theme(dialog: SettingsDialog, theme: Theme) -> None:
        dialog.theme.setCurrentIndex(dialog.theme.findData(theme))

    def check(window: MainWindow, _status: QLabel) -> None:
        set_scheme.assert_called_once_with(Qt.ColorScheme.Light)
        _edit_settings(window, lambda dialog: select_theme(dialog, "dark"), save=False)
        set_scheme.assert_called_once_with(Qt.ColorScheme.Light)

        _edit_settings(window, lambda dialog: select_theme(dialog, "dark"))
        set_scheme.assert_called_with(Qt.ColorScheme.Dark)
        assert Settings.load(path, env_file=None).theme == "dark"

        _edit_settings(window, lambda dialog: select_theme(dialog, "system"))
        assert set_scheme.call_args_list == [
            call(Qt.ColorScheme.Light),
            call(Qt.ColorScheme.Dark),
            call(Qt.ColorScheme.Unknown),
        ]
        assert Settings.load(path, env_file=None).theme == "system"

    with fake_dlx([]) as server:
        initial = Settings.model_validate(
            {"theme": "light", "deeplx_url": server.url, "deeplx_token": server.token}
        )
        run_window(initial, check)


def test_settings_save_applies_to_next_job_and_reloads(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    fixture_text: Callable[[str], str],
    vault: Mock,
) -> None:
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(fallback, "monotonic", lambda: 100.0)
    vault._read_credential.return_value = None
    official = Mock(spec=app.DeepLOfficialTranslator, name="official")
    official.name = "deepl"

    def translate(
        texts: list[str], *_args: object, dispatch: RequestDispatch
    ) -> list[TranslationResult]:
        dispatch.on_start()
        return [TranslationResult(text, Language.EN, "deepl") for text in texts]

    official.translate.side_effect = translate
    rotated_official = Mock(spec=app.DeepLOfficialTranslator)
    rotated_official.name = "deepl"
    rotated_official.translate.side_effect = official.translate.side_effect
    official_factory = Mock(side_effect=[official, rotated_official])
    monkeypatch.setattr(app, "DeepLOfficialTranslator", official_factory)
    now = 100.0
    monkeypatch.setattr("dlx_tray.providers.deeplx.monotonic", lambda: now)
    monkeypatch.setattr("dlx_tray.providers.fallback.monotonic", lambda: now)
    job_factory = Mock(wraps=app.TranslationJob)
    monkeypatch.setattr(app, "TranslationJob", job_factory)
    source = paragraphs(fixture_text("en_article.txt"))[1].text[:240]
    path = tmp_path / "settings.json"

    def edit(dialog: SettingsDialog) -> None:
        assert not dialog.formality.isEnabled()
        dialog.api_key.setText("saved-key:fx")
        dialog.formality.setCurrentIndex(dialog.formality.findData(Formality.PREFER_MORE.value))
        dialog.chunk_size.setValue(200)
        dialog.delay.setValue(0)
        dialog.dlx_concurrency.setValue(1)
        dialog.retries.setText("0")
        dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
        dialog.ui_language.setCurrentIndex(dialog.ui_language.findData("uk"))
        dialog.history_limit.setText("125")
        dialog.history_enabled.setChecked(False)

    def check(window: MainWindow, status: QLabel) -> None:
        nonlocal now
        window.source_editor.setPlainText(source)
        assert "Paragraphs: 1" in window.counter.text()
        _edit_settings(window, edit)
        job_arguments = job_factory.call_args.kwargs
        assert job_arguments["max_units"] == 200
        assert job_arguments["request_delay"] == 0
        assert job_arguments["concurrency"] == 1
        assert job_arguments["retry_policy"].max_attempts == 1
        window.translate_button.click()
        assert not window.settings_action.isEnabled()
        qtbot.waitUntil(window.settings_action.isEnabled)
        assert window.result_editor.toPlainText() == source
        assert official.translate.call_count == 2
        assert all(
            call.args[3] == Formality.PREFER_MORE for call in official.translate.call_args_list
        )

        assert "saved-key" not in path.read_text(encoding="utf-8")
        persisted = Settings.load(path, env_file=None)
        assert persisted.chunk_size == 200
        assert persisted.request_delay == 0
        assert persisted.dlx_concurrency == 1
        assert persisted.retries == 0
        assert persisted.formality == Formality.PREFER_MORE
        assert persisted.theme == "dark"
        assert persisted.ui_language == "uk"
        assert not persisted.history_enabled
        assert persisted.history_limit == 125

        def rotate(dialog: SettingsDialog) -> None:
            assert dialog.api_key.text() == "saved-key:fx"
            dialog.api_key.setText("rotated-key:fx")

        _edit_settings(window, rotate)
        official.close.assert_called_once_with()
        window.translate_button.click()
        qtbot.waitUntil(window.settings_action.isEnabled)
        assert window.result_editor.toPlainText() == source
        assert rotated_official.translate.call_count == 2

        def clear(dialog: SettingsDialog) -> None:
            assert dialog.api_key.text() == "rotated-key:fx"
            dialog.api_key.clear()

        _edit_settings(window, clear)
        rotated_official.close.assert_called_once_with()
        now += 15.0
        window.translate_button.click()
        qtbot.waitUntil(window.settings_action.isEnabled)
        assert window.result_editor.toPlainText().startswith("[UK]")
        assert official.translate.call_count == 2

    with fake_dlx(["429"]) as server:
        settings = Settings.model_validate(
            {
                "deeplx_url": server.url,
                "deeplx_token": server.token,
                "request_delay": 2.5,
                "cache_enabled": False,
            }
        )
        run_window(settings, check)

    assert vault._set_password.call_args_list == [
        call("DLX Tray", "deepl_api_key", "saved-key:fx"),
        call("DLX Tray", "deepl_api_key", "rotated-key:fx"),
    ]
    vault.delete_password.assert_called_once_with("DLX Tray", "deepl_api_key")
    assert official_factory.call_args_list == [
        call("saved-key:fx", diagnostics=ANY),
        call("rotated-key:fx", diagnostics=ANY),
    ]


@pytest.mark.parametrize("settings_fail", [True, False], ids=["json", "credential-manager"])
def test_settings_save_reports_storage_failures(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    settings_fail: bool,
) -> None:
    monkeypatch.setattr(config, "app_data_dir", lambda: tmp_path)
    write_key = Mock(side_effect=KeyringError("store unavailable"))
    monkeypatch.setattr(app, "set_deepl_api_key", write_key)
    if settings_fail:
        monkeypatch.setattr(Settings, "save", Mock(side_effect=OSError("file unavailable")))

    def edit(dialog: SettingsDialog) -> None:
        dialog.chunk_size.setValue(200)
        dialog.api_key.setText("test-key")
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        assert dialog.isVisible()
        assert dialog.save_status.text() == (
            "Could not save settings."
            if settings_fail
            else "Preferences saved. Could not update the stored API key."
        )

    def check(window: MainWindow, status: QLabel) -> None:
        window.source_editor.setPlainText("a" * 201)
        _edit_settings(window, edit, save=False)
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)

    with fake_dlx([]) as server:
        requests_sent = Mock(wraps=server.next_step)
        monkeypatch.setattr(server, "next_step", requests_sent)
        settings = Settings.model_validate({"deeplx_url": server.url, "deeplx_token": server.token})
        run_window(settings, check)
        assert requests_sent.call_count == (1 if settings_fail else 2)

    if settings_fail:
        write_key.assert_not_called()
    else:
        write_key.assert_called_once_with("test-key")
        assert Settings.load(tmp_path / "settings.json", env_file=None).chunk_size == 200


def test_dlx_streams_only_the_contiguous_prefix_with_two_workers(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    fake_dlx: FakeDLXFactory,
) -> None:
    first_started, release_first, third_started = Event(), Event(), Event()
    starts: list[tuple[str, float]] = []
    delay = 0.15

    def receive(texts: list[str], step: str) -> str:
        (text,) = texts
        starts.append((text, monotonic()))
        if text == "First":
            first_started.set()
            assert release_first.wait(5)
        elif text == "Third":
            third_started.set()
        return step

    def check(window: MainWindow, _status: QLabel) -> None:
        window.source_editor.setPlainText("First\n\nSecond\n\nThird")
        window.translate_button.click()
        try:
            qtbot.waitUntil(first_started.is_set)
            qtbot.waitUntil(third_started.is_set)
            assert window.result_editor.toPlainText() == ""
            assert window.progress_bar.value() == 0
        finally:
            release_first.set()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "[UK] First\n\n[UK] Second\n\n[UK] Third"
        assert window.progress_bar.value() == window.progress_bar.maximum() == 3

    with fake_dlx([]) as server:
        server.on_request = receive
        run_window(
            Settings.model_validate(
                {"deeplx_url": server.url, "deeplx_token": server.token, "request_delay": delay}
            ),
            check,
        )
    assert [text for text, _ in starts] == ["First", "Second", "Third"]
    assert all(
        next_start - start >= delay - 0.025 for (_, start), (_, next_start) in pairwise(starts)
    )


def test_cache_reopens_and_can_be_disabled_and_cleared_from_settings(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    fixture_text: Callable[[str], str],
) -> None:
    source = "\n\n".join(piece.text for piece in paragraphs(fixture_text("en_article.txt"))[:2])
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)

    def advance(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += seconds
        return not cancel.is_set()

    waits = Mock(side_effect=advance)
    monkeypatch.setattr(app, "event_sleep", waits)
    translated: list[str] = []

    def translate(window: MainWindow, text: str) -> str:
        window.source_editor.setPlainText(text)
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        return window.result_editor.toPlainText()

    def cold(window: MainWindow, _status: QLabel) -> None:
        translated.append(translate(window, source))
        assert translated[0].startswith("[UK] ")

    with fake_dlx(["reverse", "reverse"]) as server:
        requests_sent = Mock(wraps=server.next_step)
        monkeypatch.setattr(server, "next_step", requests_sent)
        settings = Settings.model_validate(
            {"deeplx_url": server.url, "deeplx_token": server.token, "request_delay": 2.5}
        )
        run_window(settings, cold)
        assert requests_sent.call_count == 2
        assert waits.call_count == 1

        def reopened(window: MainWindow, _status: QLabel) -> None:
            assert translate(window, source) == translated[0]
            assert requests_sent.call_count == 2
            assert waits.call_count == 1

            _edit_settings(window, lambda dialog: dialog.cache_enabled.setChecked(False))
            assert translate(window, source) != translated[0]
            assert requests_sent.call_count == 4
            assert not Settings.load(tmp_path / "settings.json", env_file=None).cache_enabled
            _edit_settings(window, lambda dialog: dialog.cache_enabled.setChecked(True))
            assert translate(window, source) == translated[0]
            assert requests_sent.call_count == 4

            monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

            def clear(dialog: SettingsDialog) -> None:
                dialog.clear_cache_button.click()
                qtbot.waitUntil(dialog.clear_cache_button.isEnabled)
                assert dialog.save_status.text() == "Translation cache cleared."

            _edit_settings(window, clear, save=False)
            assert translate(window, source) != translated[0]
            assert requests_sent.call_count == 6

        run_window(settings, reopened)
