"""Keep one application instance per profile and forward subsequent launches."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def _activate_primary(name: str, lock: QLockFile) -> bool:
    if sys.platform == "win32":
        from ctypes import windll

        pid, _, _ = lock.getLockInfo()
        if 0 < pid < 0xFFFFFFFF:
            windll.user32.AllowSetForegroundWindow(pid)
    socket = QLocalSocket()
    socket.connectToServer(name)
    connected = socket.waitForConnected(100)
    socket.abort()
    return connected


@contextmanager
def single_instance(profile: Path, *, activate: bool = True) -> Iterator[QLocalServer | None]:
    # Canonicalize the profile so case and path aliases share the same endpoint on Windows.
    profile.mkdir(parents=True, exist_ok=True)
    profile = profile.resolve()
    digest = sha256(os.fsencode(os.path.normcase(profile))).hexdigest()[:32]
    name = f"dlx-tray-{digest}"
    lock = QLockFile(str(profile / "instance.lock"))
    lock.setStaleLockTime(0)
    deadline = monotonic() + 6
    while not lock.tryLock(0):
        if lock.error() != QLockFile.LockError.LockFailedError:
            raise OSError("Could not lock the application profile")
        if not activate or _activate_primary(name, lock):
            yield None
            return
        if monotonic() >= deadline:
            raise OSError("Could not contact the running application")
        # The primary can still be creating its endpoint or releasing the profile.
        sleep(0.05)

    server = QLocalServer()
    try:
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(name)
        if not server.listen(name):
            raise OSError("Could not listen for application activation")
        yield server
    finally:
        server.close()
        lock.unlock()
