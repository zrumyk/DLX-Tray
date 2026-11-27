"""Manage the current user's Windows sign-in entry for the packaged app."""

import sys

from dlx_tray import APP_NAME

if sys.platform == "win32":
    import winreg
    from ctypes import windll
    from ctypes.wintypes import BOOL, INT, LPCWSTR

    _compare_ordinal = windll.kernel32.CompareStringOrdinal
    _compare_ordinal.argtypes = (LPCWSTR, INT, LPCWSTR, INT, BOOL)
    _compare_ordinal.restype = INT

ARGUMENT = "--autostart"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_CSTR_EQUAL = 2


def is_supported() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _command() -> str:
    return f'"{sys.executable}" {ARGUMENT}'


def _matches(key: "winreg.HKEYType", command: str) -> bool:
    try:
        value, kind = winreg.QueryValueEx(key, APP_NAME)
    except FileNotFoundError:
        return False
    return (
        kind == winreg.REG_SZ
        and isinstance(value, str)
        and _compare_ordinal(value, -1, command, -1, True) == _CSTR_EQUAL
    )


def is_registered() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            return _matches(key, _command())
    except FileNotFoundError:
        return False


def set_registered(enabled: bool) -> None:
    command = _command()
    if enabled:
        if len(command.encode("utf-16-le")) > 260 * 2:
            raise ValueError("Windows sign-in command exceeds 260 UTF-16 units")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        return

    try:
        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access) as key:
            if _matches(key, command):
                winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        return
