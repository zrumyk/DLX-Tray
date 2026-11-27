"""Exercise the Windows sign-in registry contract in a temporary key."""

import sys
from contextlib import suppress
from uuid import uuid4

import pytest

from dlx_tray import APP_NAME
from dlx_tray.services import autostart


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_current_user_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    import winreg

    key_path = rf"Software\DLX Tray autostart test {uuid4()}"
    monkeypatch.setattr(autostart, "_RUN_KEY", key_path)
    executable = "C:\\Ma\u00dfe With Spaces\\DLX Tray.exe"
    monkeypatch.setattr(sys, "executable", executable)
    try:
        assert not autostart.is_registered()
        autostart.set_registered(False)
        autostart.set_registered(True)
        assert autostart.is_registered()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            assert winreg.QueryValueEx(key, APP_NAME) == (
                f'"{executable}" --autostart',
                winreg.REG_SZ,
            )
        monkeypatch.setattr(sys, "executable", r"C:\Masse With Spaces\DLX Tray.exe")
        assert not autostart.is_registered()
        autostart.set_registered(False)
        monkeypatch.setattr(sys, "executable", executable.lower())
        assert autostart.is_registered()
        autostart.set_registered(False)
        autostart.set_registered(False)
        assert not autostart.is_registered()
    finally:
        with suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
