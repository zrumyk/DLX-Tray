"""Exercise startup and shutdown through the application event loop."""

from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray import app, secrets
from dlx_tray.config import Settings
from dlx_tray.core.models import Formality, Language, ProviderUnavailableError, TranslationResult
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.ui.main_window import MainWindow


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Mock:
    vault = Mock()
    vault.get_password.return_value = None
    monkeypatch.setattr(secrets, "_credential_manager", lambda: nullcontext(vault))
    return vault


@pytest.fixture
def run_window(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, vault: Mock
) -> Callable[[Settings, Callable[[MainWindow, QLabel], None]], None]:
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
