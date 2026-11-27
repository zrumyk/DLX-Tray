"""Verify streaming, cancellation and shutdown through the window controls."""

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray.config import UiLanguage
from dlx_tray.core import chunker
from dlx_tray.core.models import Formality, Language, TranslationResult
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.services.history import HistoryStore
from dlx_tray.ui import main_window
from dlx_tray.ui.error_dialog import ErrorDialog
from dlx_tray.ui.localization import interface_language
from dlx_tray.ui.main_window import MainWindow


@pytest.fixture
def translation_window(qtbot: QtBot) -> Iterator[Callable[[TranslationJob], MainWindow]]:
    windows: list[MainWindow] = []

    def make(job: TranslationJob) -> MainWindow:
        window = MainWindow(source=Language.UK, target=Language.EN, translation_job=job)
        windows.append(window)
        qtbot.addWidget(window)
        window.show()
        return window

    yield make
    for window in windows:
        window.stop_translation()


def test_unconfigured_server_disables_translation(qtbot: QtBot) -> None:
    reason = "Set DLX_DEEPLX_URL to translate."
    window = MainWindow(translation_unavailable_reason=reason)
    qtbot.addWidget(window)
    window.source_editor.setPlainText("A phrase")
    assert window.statusBar().currentMessage() == reason
    assert not window.translate_button.isEnabled()
    assert not window._translate_shortcut.isEnabled()


def test_history_write_failure_preserves_the_translation(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    tmp_path: Path,
) -> None:
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        window = translation_window(TranslationJob(translator=translator, request_delay=0))
        window.set_history(tmp_path, enabled=True, limit=2)
        window.source_editor.setPlainText("Hello")
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "[EN] Hello"
        assert (
            window.statusBar().currentMessage()
            == "Translation complete. Could not save to history."
        )


def test_cancel_is_unavailable_once_history_is_being_written(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saving = Event()
    release = Event()

    def open_store(path: Path, limit: int) -> HistoryStore:
        saving.set()
        if not release.wait(5):
            raise TimeoutError("Test did not release the history writer")
        return HistoryStore(path, limit)

    monkeypatch.setattr(main_window, "HistoryStore", open_store)
    path = tmp_path / "history.sqlite3"
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        window = translation_window(TranslationJob(translator=translator, request_delay=0))
        window.set_history(path, enabled=True, limit=2)
        window.source_editor.setPlainText("Hello")
        window.translate_button.click()
        try:
            qtbot.waitUntil(saving.is_set)
            qtbot.waitUntil(lambda: not window.cancel_button.isEnabled())
            assert window.progress_label.text() == "Saving translation to history…"
            window._cancel_translation()
            assert window.progress_label.text() == "Saving translation to history…"
        finally:
            release.set()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == "Translation complete."
        assert [entry.source_text for entry in HistoryStore(path, 2).list()] == ["Hello"]


@pytest.mark.parametrize("action", ["swap", "edit", "source", "target", "failed retry"])
def test_auto_detect_swap_uses_only_the_current_translation(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    action: str,
) -> None:
    with (
        fake_dlx(["ok", "401"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        window = translation_window(TranslationJob(translator=translator, request_delay=0))
        source = window.source_languages
        target = window.target_languages
        source.setCurrentIndex(source.findData(Language.AUTO.value))
        target.setCurrentIndex(target.findData(Language.PT_BR.value))
        window.source_editor.setPlainText("Hello")
        assert not window.detected_language.isVisible()
        assert not window.swap_button.isEnabled()

        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert source.currentData() == Language.AUTO
        assert window.detected_language.isVisible()
        assert window.detected_language.text() == "Detected: English"
        assert window.swap_button.isEnabled()

        if action == "swap":
            window.swap_button.click()
            assert source.currentData() == Language.PT
            assert target.currentData() == Language.EN
            assert window.source_editor.toPlainText() == "[PT-BR] Hello"
            assert window.result_editor.toPlainText() == "Hello"
        elif action == "edit":
            window.source_editor.setPlainText("Bonjour")
        elif action == "source":
            source.setCurrentIndex(source.findData(Language.FR.value))
            source.setCurrentIndex(source.findData(Language.AUTO.value))
        elif action == "target":
            target.setCurrentIndex(target.findData(Language.UK.value))
        else:
            window.translate_button.click()
            assert not window.detected_language.isVisible()
            assert not window.swap_button.isEnabled()
            dialog = _wait_for_failure_dialog(qtbot, window)
            assert "DLX authentication failed (HTTP 401)." in dialog.detail.text()
            dialog.cancel_button.click()
            qtbot.waitUntil(window.translate_button.isEnabled)
            assert window.statusBar().currentMessage() == "Translation cancelled."

        assert not window.detected_language.isVisible()
        assert window.swap_button.isEnabled() == (action == "swap")


def test_large_translation_with_loopback_server(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    fixture_text: Callable[[str], str],
) -> None:
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        job = TranslationJob(translator=translator, request_delay=0)
        window = translation_window(job)
        text = fixture_text("uk_article.txt") * 12
        assert len(text) >= 30_000
        window.source_editor.setPlainText(text)
        chunks = chunker.split(text)
        expected = chunker.join(
            chunks, [f"[EN] {c.text}" if c.translatable else "" for c in chunks]
        )

        qtbot.mouseClick(window.translate_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(window.translate_button.isEnabled, timeout=5000)
        assert window.statusBar().currentMessage() == "Translation complete."
        assert not window.detected_language.isVisible()
        assert window.progress_bar.value() == sum(c.translatable for c in chunks)
        assert window.result_editor.toPlainText() == expected


@pytest.mark.parametrize(("response", "delay"), [("429", 15), ("429-retry-after", 9)])
@pytest.mark.parametrize("language", ["en", "uk"])
def test_cancel_interrupts_retry_and_keeps_streamed_text(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
    response: str,
    delay: int,
    language: UiLanguage,
) -> None:
    now = 100.0
    first = "first" + "a" * 746
    second = "second" + "b" * 745
    third = "third" + "c" * 746
    monkeypatch.setattr("dlx_tray.providers.deeplx.monotonic", lambda: now)
    requests: list[list[str]] = []

    def record(texts: list[str], step: str) -> str:
        requests.append(texts)
        return step

    with (
        interface_language(qapp, language),
        fake_dlx(["ok", response]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        server.on_request = record
        job = TranslationJob(translator=translator, max_units=751, request_delay=0)
        window = translation_window(job)
        path = tmp_path / "history.sqlite3"
        window.set_history(path, enabled=True, limit=2)
        window.source_editor.setPlainText(f" {first}\u00a0\n\n{second} {third}")
        provider = window.findChild(QLabel, "provider_status")
        warning = window.findChild(QLabel, "rate_limit_status")
        assert provider is not None and warning is not None
        assert not provider.isVisible() and not warning.isVisible()
        window.translate_button.click()
        qtbot.waitUntil(lambda: window.progress_bar.value() == 1)
        countdown = (
            "Досягнуто ліміту запитів. Повторна спроба через {seconds} \u0441."
            if language == "uk"
            else "Rate limited. Retrying in {seconds} s."
        )
        qtbot.waitUntil(lambda: warning.text() == countdown.format(seconds=delay))
        qtbot.waitUntil(lambda: warning.text() == countdown.format(seconds=delay - 1))
        assert warning.isVisible()
        assert provider.isVisible() and provider.text() == window.tr("Provider: {name}").format(
            name="DLX"
        )
        assert window.progress_label.text() == window.tr("Chunk {processed} of {total}").format(
            processed=1, total=3
        )
        assert window.progress_bar.maximum() == 3
        assert window.result_editor.document().toRawText() == f" [EN] {first}\u00a0\u2029\u2029"
        assert not window.translate_button.isEnabled()
        assert not window.swap_button.isEnabled()
        assert window.source_editor.isReadOnly()

        qtbot.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
        assert window.progress_label.text() == window.tr("Cancelling…")
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == window.tr("Translation cancelled.")
        assert not path.exists()
        assert warning.isVisible() and warning.text() == window.tr("DLX rate limit reached.")
        assert requests == [[first], [second]]
        window.copy_button.click()
        assert QApplication.clipboard().text() == f" [EN] {first}\u00a0\n\n"
        assert window.swap_button.isEnabled()
        assert not window.source_editor.isReadOnly()

        window.source_editor.setPlainText("fresh")
        window.translate_button.click()
        local_countdown = (
            "Пауза в DLX Tray. Повторна спроба через {seconds} \u0441."
            if language == "uk"
            else "Tray cooldown. Retrying in {seconds} s."
        )
        qtbot.waitUntil(lambda: warning.text() == local_countdown.format(seconds=delay))
        window.cancel_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert requests == [[first], [second]]

        now += delay
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == window.tr("Translation complete.")
        assert window.result_editor.toPlainText() == "[EN] fresh"
        assert not warning.isVisible()


def _wait_for_failure_dialog(qtbot: QtBot, window: MainWindow) -> ErrorDialog:
    qtbot.waitUntil(lambda: isinstance(QApplication.activeModalWidget(), ErrorDialog))
    dialog = QApplication.activeModalWidget()
    assert isinstance(dialog, ErrorDialog)
    assert dialog.parentWidget() is window
    return dialog


@pytest.mark.parametrize(
    "action", ["continue", "skip", "cancel", "return", "escape", "dismiss", "close window"]
)
def test_chunk_failure_actions(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
    action: str,
) -> None:
    first, second, third = (letter * 750 + "." for letter in "abc")
    prefix = f" [EN] {first} "
    expected = {
        "continue": f"{prefix}[EN] {second[::-1]} [EN] {third}",
        "skip": f"{prefix}{second} [EN] {third[::-1]}",
    }.get(action, prefix)
    failures = ["503", "503"] if action == "continue" else ["503"]
    with (
        fake_dlx(["ok", *failures, "reverse"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        job = TranslationJob(
            translator=translator, max_units=751, request_delay=0, retry_policy=RetryPolicy(1)
        )
        window = translation_window(job)
        window.source_editor.setPlainText(f" {first} {second} {third}")
        window.translate_button.click()
        dialog = _wait_for_failure_dialog(qtbot, window)
        assert dialog.heading.text() == "Could not translate chunk 2 of 3."
        assert "provider is unavailable" in dialog.detail.text()
        assert window.result_editor.toPlainText() == prefix
        assert window.progress_bar.value() == 1
        assert not window.translate_button.isEnabled()

        if action == "continue":
            dialog.continue_button.click()
            again = _wait_for_failure_dialog(qtbot, window)
            assert again is not dialog
            assert again.heading.text() == "Could not translate chunk 2 of 3."
            again.continue_button.click()
        elif action == "skip":
            dialog.skip_button.click()
        elif action == "cancel":
            dialog.cancel_button.click()
        elif action == "return":
            assert dialog.cancel_button.isDefault()
            qtbot.keyClick(dialog, Qt.Key.Key_Return)
        elif action == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        elif action == "dismiss":
            assert dialog.close()
        else:
            assert not window.close()

        qtbot.waitUntil(window.translate_button.isEnabled)
        assert QApplication.activeModalWidget() is None
        assert window.result_editor.toPlainText() == expected
        status = (
            "Translation complete." if action in {"continue", "skip"} else "Translation cancelled."
        )
        assert window.statusBar().currentMessage() == status
        if action == "close window":
            assert not window.isVisible()
            return
        window.source_editor.setPlainText("next")
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == "Translation complete."


def test_close_waits_for_in_flight_request_without_blocking_gui(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
) -> None:
    started = Event()
    release = Event()
    calls: list[str] = []

    class BlockingTranslator:
        name = "test"

        def translate(
            self,
            texts: Sequence[str],
            source: Language,
            target: Language,
            formality: Formality = Formality.DEFAULT,
        ) -> list[TranslationResult]:
            calls.extend(texts)
            started.set()
            assert release.wait(timeout=5)
            return [TranslationResult("Translated", source, self.name)]

    window = translation_window(
        TranslationJob(translator=BlockingTranslator(), max_units=5, request_delay=0)
    )
    window.source_editor.setPlainText("first next")
    window.translate_button.click()
    try:
        qtbot.waitUntil(started.is_set)
        QTimer.singleShot(0, release.set)
        assert not window.close()
        assert window.isVisible()
        assert not release.is_set()
        qtbot.waitUntil(lambda: not window.isVisible())
        assert window.result_editor.toPlainText() == "Translated "
        assert window.statusBar().currentMessage() == "Translation cancelled."
        assert calls == ["first"]
    finally:
        release.set()
