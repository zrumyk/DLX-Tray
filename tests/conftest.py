"""Shared pytest fixtures."""

from __future__ import annotations

import gc
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent

from tests.fakes.fake_dlx import FakeDLXFactory, running_fake_dlx

# Headless Qt by default (CI, the pre-push hook); set QT_QPA_PLATFORM=windows to watch tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.txt"))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    try:
        yield
    finally:
        if (
            isinstance(item, pytest.Function)
            and "qapp" in item.fixturenames
            and QCoreApplication.instance() is not None
        ):
            # pytest-qt closes widgets and posts deleteLater() during teardown. Drain those
            # events before collecting Python cycles that might hold widget wrappers.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()


def read_fixture(name: str) -> str:
    """A fixture's exact text. Bytes are decoded without newline translation."""
    return (FIXTURES_DIR / name).read_bytes().decode("utf-8")


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """``fixture_text("uk_article.txt")`` returns the file's exact contents."""
    return read_fixture


@pytest.fixture
def fake_dlx() -> FakeDLXFactory:
    """Open a scripted DLX server on a fresh loopback port."""
    return running_fake_dlx
