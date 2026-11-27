"""Windows keyboard hook and clipboard primitives for selection translation."""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from threading import Event, Thread

from dlx_tray.config import DEFAULT_SELECTION_HOTKEY

log = logging.getLogger(__name__)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_CTRL, _SHIFT, _ALT = 0x11, 0x10, 0x12
_LEFT_WIN, _RIGHT_WIN = 0x5B, 0x5C
_MODIFIER_KEYS = {_CTRL, _SHIFT, 0xA0, 0xA1, 0xA2, 0xA3}
_KEY_UP, _INJECTED = 0x80, 0x10
_COPY_MARKER = id(_user32)
_QUIT = 0x12
_DOUBLE_COPY_MS = 650


class _KeyboardEvent(ctypes.Structure):
    vk: int
    flags: int
    time: int
    extra: int
    _fields_ = (
        ("vk", wintypes.DWORD),
        ("scan", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    )


class _KeyInput(ctypes.Structure):
    _fields_ = (
        ("vk", wintypes.WORD),
        ("scan", wintypes.WORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    )


class _MouseInput(ctypes.Structure):
    _fields_ = (
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
        ("mouse_data", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    )


class _InputData(ctypes.Union):
    _fields_ = (("keyboard", _KeyInput), ("mouse", _MouseInput))


class _Input(ctypes.Structure):
    _fields_ = (("kind", wintypes.DWORD), ("data", _InputData))


_user32.SetWindowsHookExW.argtypes = (
    ctypes.c_int,
    _hook_proc,
    wintypes.HINSTANCE,
    wintypes.DWORD,
)
_user32.SetWindowsHookExW.restype = wintypes.HANDLE
_user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
_user32.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = wintypes.SHORT
_user32.PeekMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
)
_user32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)
_user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
_user32.DispatchMessageW.restype = ctypes.c_ssize_t
_user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetClipboardOwner.restype = wintypes.HWND
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def foreground() -> int | None:
    return _user32.GetForegroundWindow()


def clipboard_sequence() -> int:
    return _user32.GetClipboardSequenceNumber()


def _process_id(window: int | None) -> int:
    process = wintypes.DWORD()
    if window:
        _user32.GetWindowThreadProcessId(window, ctypes.byref(process))
    return process.value


def clipboard_belongs_to(window: int) -> bool:
    owner = _process_id(_user32.GetClipboardOwner())
    return bool(owner and owner == _process_id(window))


def _down(key: int) -> bool:
    return bool(_user32.GetAsyncKeyState(key) & 0x8000)


def modifiers_down() -> bool:
    return any(_down(key) for key in (_CTRL, _SHIFT, _ALT, _LEFT_WIN, _RIGHT_WIN))


def copy_selection() -> bool:
    inputs = (_Input * 4)(
        *(
            _Input(1, _InputData(keyboard=_KeyInput(key, 0, flags, 0, _COPY_MARKER)))
            for key, flags in ((_CTRL, 0), (ord("C"), 0), (ord("C"), 2), (_CTRL, 2))
        )
    )
    return _user32.SendInput(len(inputs), inputs, ctypes.sizeof(_Input)) == len(inputs)


@dataclass(frozen=True, slots=True)
class Capture:
    window: int
    sequence: int
    input_serial: int
    clipboard_updates: int
    copy: bool = False


class KeyboardHook:
    def __init__(
        self,
        shortcut: str,
        activate: Callable[[Capture], None],
        clipboard_updates: Callable[[], int],
    ) -> None:
        self._double_copy = shortcut == DEFAULT_SELECTION_HOTKEY
        self._key = ord("C" if self._double_copy else shortcut[-1])
        self._activate = activate
        self._clipboard_updates = clipboard_updates
        self._pressed = False
        self._consumed = False
        self._first: tuple[int, int, int, int] | None = None
        self.input_serial = 0
        self._paused = False
        self._ready = Event()
        self._thread_id = 0
        self._thread = Thread(target=self._run, name="DLX selection hotkey", daemon=True)

    def start(self) -> bool:
        self._thread.start()
        self._ready.wait()
        return bool(self._thread_id)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._first = None
            self.input_serial += 1

    def stop(self) -> None:
        if self._thread_id and not _user32.PostThreadMessageW(self._thread_id, _QUIT, 0, 0):
            if self._thread.is_alive():
                log.warning("Could not stop the selection hotkey thread")
            return
        self._thread.join()

    def _run(self) -> None:
        @_hook_proc
        def on_key(code: int, message: int, data: int) -> int:
            if code == 0:
                event = _KeyboardEvent.from_address(data)
                own_copy = event.flags & _INJECTED and event.extra == _COPY_MARKER
                if not own_copy and self._on_key(event):
                    return 1
            return _user32.CallNextHookEx(None, code, message, data)

        message = wintypes.MSG()
        # Create the queue before publishing its thread ID to stop().
        _user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        hook = _user32.SetWindowsHookExW(13, on_key, _kernel32.GetModuleHandleW(None), 0)
        if not hook:
            log.warning(
                "Could not install selection hotkey (Windows error %s)", ctypes.get_last_error()
            )
            self._ready.set()
            return
        try:
            self._thread_id = _kernel32.GetCurrentThreadId()
            self._ready.set()
            while (status := _user32.GetMessageW(ctypes.byref(message), None, 0, 0)) > 0:
                _user32.DispatchMessageW(ctypes.byref(message))
            if status < 0:
                log.warning("Selection hotkey message loop failed")
        finally:
            _user32.UnhookWindowsHookEx(hook)
            self._thread_id = 0

    def _on_key(self, event: _KeyboardEvent) -> bool:
        if event.vk != self._key:
            if not event.flags & _KEY_UP and event.vk not in _MODIFIER_KEYS:
                self.input_serial += 1
                self._first = None
            return False
        if event.flags & _KEY_UP:
            consumed = self._consumed
            self._pressed = self._consumed = False
            return consumed
        if self._pressed:
            return self._consumed
        self._pressed = True
        self.input_serial += 1
        if (
            self._paused
            or not _down(_CTRL)
            or _down(_SHIFT) == self._double_copy
            or any(_down(key) for key in (_ALT, _LEFT_WIN, _RIGHT_WIN))
            or not (window := foreground())
        ):
            self._first = None
            return False
        if not self._double_copy:
            self._consumed = True
            self._activate(
                Capture(window, 0, self.input_serial, self._clipboard_updates(), copy=True)
            )
        else:
            first = self._first
            if (
                first
                and first[0] == window
                and (event.time - first[1]) % (1 << 32) <= _DOUBLE_COPY_MS
            ):
                self._first = None
                self._consumed = True
                self._activate(Capture(window, first[2], self.input_serial, first[3]))
            else:
                self._first = (window, event.time, clipboard_sequence(), self._clipboard_updates())
        return self._consumed
