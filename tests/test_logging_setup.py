"""Verify that the application log filters records and bounds retained history."""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from dlx_tray import APP_NAME
from dlx_tray.logging_setup import file_logging


def test_application_log_uses_appdata_and_filters_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    application_logger = logging.getLogger("dlx_tray")
    logger = application_logger.getChild("test")
    previous_level = application_logger.level
    previous_handlers = application_logger.handlers.copy()
    with file_logging():
        logger.debug("hidden debug")
        logger.info("Привіт")
        logging.getLogger("httpx").warning("external HTTP details")

    assert application_logger.level == previous_level
    assert application_logger.handlers == previous_handlers
    path = tmp_path / APP_NAME / "logs" / "dlx-tray.log"
    assert path.read_text(encoding="utf-8").split(" INFO ")[1] == "dlx_tray.test Привіт\n"


def test_rotates_and_retains_two_backups(tmp_path: Path) -> None:
    logger = logging.getLogger("dlx_tray.test")
    with file_logging("DEBUG", directory=tmp_path):
        for index in range(4):
            logger.debug("record %d %s", index, "x" * 600_000)

    for suffix, index in [("", 3), (".1", 2), (".2", 1)]:
        path = tmp_path / f"dlx-tray.log{suffix}"
        assert path.stat().st_size < 1024**2
        assert f"record {index}" in path.read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("dlx-tray.log*"))) == 3


def test_rotates_while_another_instance_is_running(tmp_path: Path) -> None:
    child_script = """
import logging
import sys
from pathlib import Path
from dlx_tray.logging_setup import file_logging

with file_logging(directory=Path(sys.argv[1])):
    logging.getLogger('dlx_tray.test').info('child record')
"""
    path = tmp_path / "dlx-tray.log"
    path.write_bytes(b"x" * 1024**2)
    with file_logging(directory=tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", child_script, str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert not result.stderr
        logging.getLogger("dlx_tray.test").info("parent record after rotation")

    contents = path.read_text(encoding="utf-8")
    assert "child record" in contents
    assert "parent record after rotation" in contents
    assert (tmp_path / "dlx-tray.log.1").stat().st_size == 1024**2
