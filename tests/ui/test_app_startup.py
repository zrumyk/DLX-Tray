"""Exercise startup and shutdown through the application event loop."""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray import app
from dlx_tray.config import Settings
from dlx_tray.core.models import Formality, Language, ProviderUnavailableError, TranslationResult
from dlx_tray.services.deeplx_process import DLXProcess
from dlx_tray.ui.main_window import MainWindow


@pytest.fixture
def run_window(
    qapp: QApplication, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
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
def test_uses_official_api_when_dlx_is_unavailable(
    run_window: Callable[[Settings, Callable[[MainWindow, QLabel], None]], None],
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    fake_dlx: FakeDLXFactory,
    startup_fails: bool,
    tmp_path: Path,
) -> None:
    official = Mock(spec=app.DeepLOfficialTranslator)
    official.name = "deepl"
    official.translate.return_value = [TranslationResult("Привіт", Language.EN, "deepl")]
    official_factory = Mock(return_value=official)
    monkeypatch.setattr(app, "DeepLOfficialTranslator", official_factory)
    monkeypatch.setenv("DLX_DEEPL_API_KEY", "test-key:fx")

    def check(window: MainWindow, status: QLabel) -> None:
        window.source_editor.setPlainText("Hello")
        qtbot.waitUntil(window.translate_button.isEnabled)
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "Привіт"
        assert status.text() == (
            "DLX unavailable" if startup_fails else "Using external DLX server"
        )
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
        settings = Settings.load(tmp_path / "settings.json", env_file=None)
        run_window(settings, check)

    official_factory.assert_called_once_with("test-key:fx")
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
