"""Read a fresh copy from the foreground application after the global shortcut."""

from __future__ import annotations

import sys
from dataclasses import replace
from time import monotonic

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QWidget

if sys.platform == "win32":
    from dlx_tray.ui import _win32_hotkey as native


class SelectionHotkey(QObject):
    selection_ready = Signal(str)
    _activated = Signal(object, object)

    def __init__(self, window: QWidget | None = None) -> None:
        super().__init__(window)
        self._hook: native.KeyboardHook | None = None
        self._generation = object()
        self._capture: native.Capture | None = None
        self._first: str | None = None
        self._deadline = 0.0
        self._paused = False
        self._clipboard_updates = 0
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._poll)
        self._activated.connect(self._begin, Qt.ConnectionType.QueuedConnection)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.dataChanged.connect(self._data_changed)
        if window is not None:
            window.installEventFilter(self)

    def configure(self, shortcut: str) -> bool:
        self.stop()
        if sys.platform != "win32" or not shortcut:
            return True
        generation = self._generation
        self._hook = native.KeyboardHook(
            shortcut,
            lambda capture: self._activated.emit(generation, capture),
            lambda: self._clipboard_updates,
        )
        self._hook.set_paused(self._paused)
        return self._hook.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.WindowBlocked, QEvent.Type.WindowUnblocked):
            self._paused = event.type() == QEvent.Type.WindowBlocked
            if self._hook is not None:
                self._hook.set_paused(self._paused)
            if self._paused:
                self._clear()
        return super().eventFilter(watched, event)

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
        self._first = None

    @Slot()
    def _data_changed(self) -> None:
        self._clipboard_updates += 1

    @Slot(object, object)
    def _begin(self, generation: object, capture: native.Capture) -> None:
        if generation is not self._generation or QApplication.activeModalWidget() is not None:
            return
        self._clear()
        self._capture = capture
        self._deadline = monotonic() + 1.0
        self._timer.start()

    @Slot()
    def _poll(self) -> None:
        capture = self._capture
        if monotonic() >= self._deadline:
            self._clear()
            return
        if capture is None:
            return
        if not self._is_current(capture):
            self._clear()
            return
        if capture.copy:
            if not native.modifiers_down():
                self._capture = replace(
                    capture,
                    sequence=native.clipboard_sequence(),
                    clipboard_updates=self._clipboard_updates,
                    copy=False,
                )
                if native.copy_selection():
                    self._deadline = monotonic() + 1.0
                else:
                    self._clear()
            return

        sequence = native.clipboard_sequence()
        if not sequence or not native.clipboard_belongs_to(capture.window):
            return
        # A clipboard update can arrive before Windows renders the selected text.
        if sequence == capture.sequence and self._clipboard_updates == capture.clipboard_updates:
            return
        text = QApplication.clipboard().text()
        if (
            sequence != capture.sequence
            and native.clipboard_sequence() == sequence
            and native.clipboard_belongs_to(capture.window)
            and self._is_current(capture)
            and text.strip()
        ):
            self._accept_sample(capture, text)

    def _is_current(self, capture: native.Capture) -> bool:
        return (
            self._hook is not None
            and self._hook.input_serial == capture.input_serial
            and native.foreground() == capture.window
            and not native.pointer_buttons_down()
            and QApplication.activeModalWidget() is None
        )

    def _accept_sample(self, capture: native.Capture, text: str) -> None:
        if self._first is not None:
            matches = text == self._first
            self._clear()
            if matches:
                self.selection_ready.emit(text)
        else:
            self._first = text
            self._capture = replace(capture, copy=True)
