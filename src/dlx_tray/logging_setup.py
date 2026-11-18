"""Attach a rotating file handler for the lifetime of the DLX Tray application."""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from dlx_tray.config import LogLevel, app_data_dir


@contextmanager
def _log_lock(path: Path) -> Iterator[None]:
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


class _SharedRotatingFileHandler(RotatingFileHandler):
    """Close the log between writes so another process can rotate it on Windows."""

    def __init__(self, path: Path) -> None:
        self.lock_path = path.with_suffix(".lock")
        # Check permissions before startup succeeds, without holding the log open.
        with _log_lock(self.lock_path), path.open("a", encoding="utf-8"):
            pass
        super().__init__(path, maxBytes=1024**2, backupCount=2, encoding="utf-8", delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with _log_lock(self.lock_path):
                try:
                    super().emit(record)
                finally:
                    stream = self.stream
                    self.stream = None
                    if stream is not None:
                        stream.close()
        except OSError:
            self.handleError(record)


@contextmanager
def file_logging(level: LogLevel = "INFO", *, directory: Path | None = None) -> Iterator[None]:
    """Keep the active UTF-8 log plus up to two backups of about 1 MiB each."""
    if directory is None:
        directory = app_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = _SharedRotatingFileHandler(directory / "dlx-tray.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.setLevel(level)

    logger = logging.getLogger("dlx_tray")
    old_level = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()
