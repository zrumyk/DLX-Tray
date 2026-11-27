"""Cover global shortcut recognition and stale clipboard rejection."""

import sys
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from pytestqt.qtbot import QtBot

from dlx_tray.config import DEFAULT_SELECTION_HOTKEY
from dlx_tray.ui.selection_hotkey import SelectionHotkey

if sys.platform == "win32":
    from dlx_tray.ui import _win32_hotkey as native

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows keyboard hook")


def test_double_copy_requires_two_distinct_presses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "foreground", lambda: 10)
    monkeypatch.setattr(native, "clipboard_sequence", lambda: 30)
    monkeypatch.setattr(native, "_down", lambda key: key == native._CTRL)
    activate = Mock()
    hook = native.KeyboardHook(DEFAULT_SELECTION_HOTKEY, activate, lambda: 2)

    def key(time: int, flags: int = 0) -> bool:
        return hook._on_key(native._KeyboardEvent(ord("C"), 0, flags, time, 0))

    assert not key(1000)
    assert not key(1300)
    activate.assert_not_called()
    assert not key(1310, native._KEY_UP)
    assert not key(1320)
    assert not key(1330, native._KEY_UP)
    activate.assert_called_once_with(native.Capture(10, 30, 2, 2))


@pytest.mark.parametrize(
    ("second_text", "intervening_input"),
    [
        ("New selection", None),
        ("Unrelated clipboard write", None),
        ("New selection", "keyboard"),
        ("New selection", "pointer"),
    ],
)
def test_requires_matching_fresh_copies_from_foreground_application(
    second_text: str, intervening_input: str | None, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    hotkey = SelectionHotkey()
    hook = native.KeyboardHook(DEFAULT_SELECTION_HOTKEY, Mock(), lambda: 0)
    hotkey._hook = hook
    monkeypatch.setattr(native, "foreground", lambda: 10)
    monkeypatch.setattr(native, "modifiers_down", lambda: False)
    pointer_buttons = Mock(return_value=False)
    monkeypatch.setattr(native, "pointer_buttons_down", pointer_buttons)
    copy = Mock(return_value=True)
    monkeypatch.setattr(native, "copy_selection", copy)
    sequence = Mock(return_value=30)
    owner_matches = Mock(return_value=False)
    monkeypatch.setattr(native, "clipboard_sequence", sequence)
    monkeypatch.setattr(native, "clipboard_belongs_to", owner_matches)
    clipboard = QApplication.clipboard()
    assert clipboard is not None
    clipboard.setText("Old clipboard contents")

    with qtbot.assertNotEmitted(hotkey.selection_ready):
        hotkey._begin(hotkey._generation, native.Capture(10, 30, 0, hotkey._clipboard_updates))
        hotkey._poll()
        sequence.return_value = 31
        hotkey._poll()

        clipboard.setText("New selection")
        owner_matches.return_value = True
        hotkey._poll()
        hotkey._poll()
        copy.assert_called_once()

    clipboard.setText(second_text)
    sequence.return_value = 32
    if intervening_input == "keyboard":
        hook.input_serial += 1
    elif intervening_input == "pointer":
        pointer_buttons.return_value = True
    if second_text == "New selection" and not intervening_input:
        with qtbot.waitSignal(hotkey.selection_ready, timeout=500) as ready:
            hotkey._poll()
        assert ready.args == ["New selection"]
    else:
        with qtbot.assertNotEmitted(hotkey.selection_ready):
            hotkey._poll()
    assert hotkey._capture is None


def test_delayed_rendering_reads_text_after_update(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    hotkey = SelectionHotkey()
    hotkey._hook = native.KeyboardHook(DEFAULT_SELECTION_HOTKEY, Mock(), lambda: 0)
    monkeypatch.setattr(native, "foreground", lambda: 10)
    monkeypatch.setattr(native, "clipboard_belongs_to", lambda _window: True)
    monkeypatch.setattr(native, "pointer_buttons_down", lambda: False)
    sequence = Mock(return_value=30)
    monkeypatch.setattr(native, "clipboard_sequence", sequence)
    clipboard = QApplication.clipboard()
    assert clipboard is not None

    def render() -> str:
        sequence.return_value = 31
        return "Rendered selection"

    read = Mock(side_effect=render)
    monkeypatch.setattr(clipboard, "text", read)
    hotkey._begin(hotkey._generation, native.Capture(10, 30, 0, hotkey._clipboard_updates))
    with qtbot.assertNotEmitted(hotkey.selection_ready):
        hotkey._poll()
        read.assert_not_called()
        hotkey._data_changed()
        hotkey._poll()
        read.assert_called_once()
        hotkey._poll()
        assert hotkey._first == "Rendered selection"
    hotkey._clear()


@pytest.mark.parametrize("visible", [True, False])
def test_modal_dialog_pauses_custom_shortcut(
    visible: bool, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = QWidget()
    qtbot.addWidget(main)
    main.show()
    main.setVisible(visible)
    hotkey = SelectionHotkey(main)
    activate = Mock()
    hook = native.KeyboardHook("Ctrl+Shift+Y", activate, lambda: 0)
    hotkey._hook = hook
    monkeypatch.setattr(native, "foreground", lambda: 10)
    monkeypatch.setattr(native, "_down", lambda key: key in (native._CTRL, native._SHIFT))
    dialog = QDialog(main)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.show()

    assert not hook._on_key(native._KeyboardEvent(ord("Y"), 0, 0, 1000, 0))
    assert not hook._on_key(native._KeyboardEvent(ord("Y"), 0, native._KEY_UP, 1010, 0))
    activate.assert_not_called()

    dialog.close()
    assert hook._on_key(native._KeyboardEvent(ord("Y"), 0, 0, 1020, 0))
