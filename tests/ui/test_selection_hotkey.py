"""Cover global shortcut recognition and stale clipboard rejection."""

import sys
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication
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
    hook = native.KeyboardHook(DEFAULT_SELECTION_HOTKEY, activate)

    def key(time: int, flags: int = 0) -> bool:
        return hook._on_key(native._KeyboardEvent(ord("C"), 0, flags, time, 0))

    assert not key(1000)
    assert not key(1300)
    activate.assert_not_called()
    assert not key(1310, native._KEY_UP)
    assert not key(1320)
    activate.assert_called_once_with(native.Capture(10, 30, copy=False))


def test_requires_new_clipboard_text_from_foreground_application(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    hotkey = SelectionHotkey()
    monkeypatch.setattr(native, "foreground", lambda: 10)
    sequence = Mock(return_value=30)
    owner_matches = Mock(return_value=False)
    monkeypatch.setattr(native, "clipboard_sequence", sequence)
    monkeypatch.setattr(native, "clipboard_belongs_to", owner_matches)
    clipboard = QApplication.clipboard()
    assert clipboard is not None
    clipboard.setText("Old clipboard contents")

    with qtbot.assertNotEmitted(hotkey.selection_ready):
        hotkey._begin(hotkey._generation, native.Capture(10, 30, copy=False))
        hotkey._poll()
        sequence.return_value = 31
        hotkey._poll()

    clipboard.setText("New selection")
    owner_matches.return_value = True
    with qtbot.waitSignal(hotkey.selection_ready, timeout=500) as ready:
        hotkey._poll()
    assert ready.args == ["New selection"]
