"""Read a fresh copy from the foreground application after the global shortcut."""

from __future__ import annotations

import sys
from dataclasses import replace
from time import monotonic

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

if sys.platform == "win32":
    from dlx_tray.ui import _win32_hotkey as native


class SelectionHotkey(QObject):
    selection_ready = Signal(str)
    _activated = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self._hook: native.KeyboardHook | None = None
        self._generation = object()
        self._capture: native.Capture | None = None
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)
        self._activated.connect(self._begin, Qt.ConnectionType.QueuedConnection)

    def configure(self, shortcut: str) -> bool:
        self.stop()
        if sys.platform != "win32" or not shortcut:
            return True
        generation = self._generation
        self._hook = native.KeyboardHook(
            shortcut, lambda capture: self._activated.emit(generation, capture)
        )
        return self._hook.start()

    @Slot()
    def stop(self) -> None:
        self._generation = object()
        self._clear()
        if self._hook is not None:
            self._hook.stop()
            self._hook = None

    def _clear(self) -> None:
        self._timer.stop()
        self._capture = None

    @Slot(object, object)
    def _begin(self, generation: object, capture: native.Capture) -> None:
        if generation is not self._generation or QApplication.activeModalWidget() is not None:
            return
        self._capture = capture
        self._deadline = monotonic() + 1.0
        self._timer.start()

    @Slot()
    def _poll(self) -> None:
        capture = self._capture
        if capture is None:
            return
        if (
            monotonic() >= self._deadline
            or native.foreground() != capture.window
            or QApplication.activeModalWidget() is not None
        ):
            self._clear()
            return
        if capture.copy:
            if not native.modifiers_down():
                self._capture = replace(capture, sequence=native.clipboard_sequence(), copy=False)
                if native.copy_selection():
                    self._deadline = monotonic() + 1.0
                else:
                    self._clear()
            return

        sequence = native.clipboard_sequence()
        if (
            not sequence
            or sequence == capture.sequence
            or not native.clipboard_belongs_to(capture.window)
        ):
            return
        text = QApplication.clipboard().text()
        if (
            native.clipboard_sequence() == sequence
            and native.clipboard_belongs_to(capture.window)
            and native.foreground() == capture.window
        ):
            self._clear()
            if text.strip():
                self.selection_ready.emit(text)
