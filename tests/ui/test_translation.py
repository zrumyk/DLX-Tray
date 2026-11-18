"""Verify streaming, cancellation and shutdown through the window controls."""

from collections.abc import Callable, Iterator, Sequence
from threading import Event

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray.core import chunker
from dlx_tray.core.models import Formality, Language, TranslationResult
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.providers.deeplx import DeeplxTranslator
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
            qtbot.waitUntil(window.translate_button.isEnabled)
            assert (
                window.statusBar().currentMessage()
                == "Translation failed. Check DLX and try again."
            )

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


def test_cancel_interrupts_retry_and_keeps_streamed_text(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
) -> None:
    waiting = Event()

    def sleep(seconds: float, cancel: Event) -> bool:
        assert seconds == 9.0
        waiting.set()
        return not cancel.wait(seconds)

    with (
        fake_dlx(["ok", "429-retry-after"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        job = TranslationJob(translator=translator, max_units=6, request_delay=0, sleep=sleep)
        window = translation_window(job)
        window.source_editor.setPlainText(" first\u00a0\n\nsecond third")
        window.translate_button.click()
        qtbot.waitUntil(waiting.is_set)
        qtbot.waitUntil(lambda: window.progress_bar.value() == 1)
        assert window.progress_label.text() == "Chunk 1 of 3"
        assert window.progress_bar.maximum() == 3
        assert window.result_editor.document().toRawText() == " [EN] first\u00a0\u2029\u2029"
        assert not window.translate_button.isEnabled()
        assert not window.swap_button.isEnabled()
        assert window.source_editor.isReadOnly()

        qtbot.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
        assert window.progress_label.text() == "Cancelling…"
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == "Translation cancelled."
        window.copy_button.click()
        assert QApplication.clipboard().text() == " [EN] first\u00a0\n\n"
        assert window.swap_button.isEnabled()
        assert not window.source_editor.isReadOnly()

        window.source_editor.setPlainText("fresh")
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == "Translation complete."
        assert window.result_editor.toPlainText() == "[EN] fresh"


def test_failure_keeps_prefix_and_allows_another_job(
    qtbot: QtBot,
    translation_window: Callable[[TranslationJob], MainWindow],
    fake_dlx: FakeDLXFactory,
) -> None:
    with (
        fake_dlx(["ok", "503", "reverse"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        job = TranslationJob(
            translator=translator, max_units=5, request_delay=0, retry_policy=RetryPolicy(1)
        )
        window = translation_window(job)
        window.source_editor.setPlainText("first next")
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.statusBar().currentMessage() == "Translation failed. Check DLX and try again."
        assert window.result_editor.toPlainText() == "[EN] first "

        window.source_editor.setPlainText("next")
        window.translate_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "[EN] txen"


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
