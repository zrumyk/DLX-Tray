"""Exercise the main window's translation controls."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from dlx_tray.core.models import Language
from dlx_tray.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot: QtBot) -> MainWindow:
    window = MainWindow(source=Language.DE, target=Language.EN_GB, chunk_size=200)
    qtbot.addWidget(window)
    return window


def test_language_choices(window: MainWindow) -> None:
    source = window.source_languages
    target = window.target_languages
    assert source.currentData() == Language.DE
    assert target.currentData() == Language.EN_GB
    assert source.itemData(0) == Language.AUTO
    assert {source.itemData(i) for i in range(source.count())} == {
        lang for lang in Language if lang.can_be_source
    }
    assert {target.itemData(i) for i in range(target.count())} == {
        lang for lang in Language if lang.can_be_target
    }


def test_counter_counts_utf16_units_and_translatable_chunks(window: MainWindow) -> None:
    assert window.counter.text() == "0 characters · 0 chunks"
    assert not window.translate_button.isEnabled()
    window.source_editor.setPlainText(" \n\t")
    assert window.counter.text() == "3 characters · 0 chunks"
    assert not window.translate_button.isEnabled()
    window.source_editor.setPlainText("  " + "a" * 199 + "\U0001f600")
    assert window.counter.text() == "203 characters · 2 chunks"
    assert window.translate_button.isEnabled()


def test_button_and_shortcut_emit_translation_request(window: MainWindow, qtbot: QtBot) -> None:
    window.show()
    window.activateWindow()
    window.source_editor.setFocus()
    qtbot.waitUntil(window.source_editor.hasFocus)
    text = "  Hallo\u00a0Welt\n"
    window.source_editor.setPlainText(text)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        with qtbot.waitSignal(window.translation_requested) as request:
            qtbot.keyClick(window.source_editor, key, Qt.KeyboardModifier.ControlModifier)
        assert request.args == [text, Language.DE, Language.EN_GB]

    qtbot.keyClick(window.source_editor, Qt.Key.Key_Return)
    with qtbot.waitSignal(window.translation_requested) as request:
        qtbot.mouseClick(window.translate_button, Qt.MouseButton.LeftButton)
    assert request.args == ["\n" + text, Language.DE, Language.EN_GB]


def test_swap_uses_base_source_language_and_exchanges_texts(window: MainWindow) -> None:
    window.source_editor.setPlainText("Guten\u00a0Tag")
    window.result_editor.setPlainText("Hello\n\nthere")
    window.swap_button.click()
    assert window.source_languages.currentData() == Language.EN
    assert window.target_languages.currentData() == Language.DE
    assert window.source_editor.toPlainText() == "Hello\n\nthere"
    assert window.result_editor.document().toRawText() == "Guten\u00a0Tag"


def test_swap_keeps_source_text_when_result_is_empty(window: MainWindow) -> None:
    window.source_editor.setPlainText("Hallo")
    window.swap_button.click()
    assert window.source_languages.currentData() == Language.EN
    assert window.target_languages.currentData() == Language.DE
    assert window.source_editor.toPlainText() == "Hallo"
    assert window.result_editor.document().isEmpty()


def test_swap_is_disabled_for_auto_detect(window: MainWindow) -> None:
    window.source_languages.setCurrentIndex(window.source_languages.findData(Language.AUTO.value))
    assert not window.swap_button.isEnabled()


def test_copy_uses_the_entire_read_only_result(window: MainWindow) -> None:
    assert window.result_editor.isReadOnly()
    assert not window.copy_button.isEnabled()
    text = "Hello\u00a0there\n\nGoodbye"
    window.result_editor.setPlainText(text)
    window.copy_button.click()
    assert QApplication.clipboard().text() == text
    window.result_editor.clear()
    assert not window.copy_button.isEnabled()
