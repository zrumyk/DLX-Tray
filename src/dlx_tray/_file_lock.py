"""Serialize short file operations across application instances."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    # All processes lock the same byte. Keep the lock file in place between writes.
    with path.open("a+b") as lock:
        lock.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
