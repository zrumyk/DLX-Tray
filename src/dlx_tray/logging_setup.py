"""Attach a rotating file handler for the lifetime of the DLX Tray application."""

from collections.abc import Iterator
from contextlib import contextmanager
from logging import Formatter, getLogger
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dlx_tray.config import LogLevel, app_data_dir


@contextmanager
def file_logging(level: LogLevel = "INFO", *, directory: Path | None = None) -> Iterator[None]:
    """Keep the active UTF-8 log plus up to two backups of about 1 MiB each."""
    if directory is None:
        directory = app_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "dlx-tray.log", maxBytes=1024**2, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.setLevel(level)

    logger = getLogger("dlx_tray")
    old_level = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()
