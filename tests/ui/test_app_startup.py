"""Exercise startup and shutdown through the application event loop."""

import json
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call

import pytest
from keyring.errors import KeyringError
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray import app, config, secrets
from dlx_tray.config import Settings, Theme
from dlx_tray.core.models import Formality, Language, ProviderUnavailableError, TranslationResult
from dlx_tray.providers import fallback
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.services.history import HistoryStore
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
    official.translate.return_value = [TranslationResult("Привіт", Language.EN, "deepl")]
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
    official_factory.assert_called_once_with(expected_keys[key_source])
    official.translate.assert_called_once_with(
        ["Hello"], Language.AUTO, Language.UK, Formality.DEFAULT
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
    official.translate.side_effect = lambda texts, *_: [
        TranslationResult(text, Language.EN, "deepl") for text in texts
    ]
    rotated_official = Mock(spec=app.DeepLOfficialTranslator)
    rotated_official.name = "deepl"
    rotated_official.translate.side_effect = official.translate.side_effect
    official_factory = Mock(side_effect=[official, rotated_official])
    monkeypatch.setattr(app, "DeepLOfficialTranslator", official_factory)
    job_factory = Mock(wraps=app.TranslationJob)
    monkeypatch.setattr(app, "TranslationJob", job_factory)
    source = fixture_text("en_article.txt")[:240]
    path = tmp_path / "settings.json"

    def edit(dialog: SettingsDialog) -> None:
        assert not dialog.formality.isEnabled()
        dialog.api_key.setText("saved-key:fx")
        dialog.formality.setCurrentIndex(dialog.formality.findData(Formality.PREFER_MORE.value))
        dialog.chunk_size.setValue(200)
        dialog.delay.setValue(0)
        dialog.retries.setText("0")
        dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
        dialog.ui_language.setCurrentIndex(dialog.ui_language.findData("uk"))
        dialog.history_limit.setText("125")
        dialog.history_enabled.setChecked(False)

    def check(window: MainWindow, status: QLabel) -> None:
        window.source_editor.setPlainText(source)
        assert "1 chunks" in window.counter.text()
        _edit_settings(window, edit)
        assert "2 chunks" in window.counter.text()
        job_arguments = job_factory.call_args.kwargs
        assert job_arguments["max_units"] == 200
        assert job_arguments["request_delay"] == 0
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
        window.translate_button.click()
        qtbot.waitUntil(window.settings_action.isEnabled)
        assert window.result_editor.toPlainText().startswith("[UK]")
        assert official.translate.call_count == 2

    with fake_dlx(["429"]) as server:
        settings = Settings.model_validate({"deeplx_url": server.url, "deeplx_token": server.token})
        run_window(settings, check)

    assert vault._set_password.call_args_list == [
        call("DLX Tray", "deepl_api_key", "saved-key:fx"),
        call("DLX Tray", "deepl_api_key", "rotated-key:fx"),
    ]
    vault.delete_password.assert_called_once_with("DLX Tray", "deepl_api_key")
    assert official_factory.call_args_list == [call("saved-key:fx"), call("rotated-key:fx")]


@pytest.mark.parametrize("settings_fail", [True, False], ids=["json", "credential-manager"])
def test_settings_save_reports_storage_failures(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
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
        assert window.counter.text().endswith("1 chunks" if settings_fail else "2 chunks")

    with fake_dlx([]) as server:
        run_window(Settings.model_validate({"deeplx_url": server.url}), check)

    if settings_fail:
        write_key.assert_not_called()
    else:
        write_key.assert_called_once_with("test-key")
        assert Settings.load(tmp_path / "settings.json", env_file=None).chunk_size == 200
