"""Process helpers for the DLXProcess tests (Windows)."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
FAKE_SERVER = HERE / "fake_dlx_server.py"
HOLDER = HERE / "dlx_holder.py"
FAKE_COMMAND = [sys.executable, str(FAKE_SERVER)]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def answers(base_url: str) -> bool:
    try:
        return httpx.get(base_url + "/", timeout=1, trust_env=False).status_code == 200
    except httpx.TransportError:
        return False


def kill_holder_and_check_no_orphan(command: list[str]) -> None:
    """Run ``dlx_holder.py`` with ``command``, hard-kill it, and assert DLX died with it."""
    holder = subprocess.Popen(
        [sys.executable, str(HOLDER), *command],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline().split()
        assert len(line) == 3, f"holder printed {line!r}, exit code {holder.poll()}"
        base_url, dlx_pid, holder_pid = line[0], int(line[1]), int(line[2])
        assert answers(base_url)
        os.kill(holder_pid, signal.SIGTERM)  # TerminateProcess: no cleanup code runs
        assert wait_until(lambda: not pid_alive(holder_pid))
        assert wait_until(lambda: not answers(base_url)), "DLX still answers after the kill"
        assert wait_until(lambda: not pid_alive(dlx_pid)), f"orphan DLX pid {dlx_pid}"
    finally:
        holder.kill()
        holder.wait(timeout=10)
