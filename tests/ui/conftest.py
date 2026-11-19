"""Keep Qt widget cleanup on the GUI thread between UI tests."""

import gc
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QCoreApplication, QEvent


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown() -> Iterator[None]:
    try:
        yield
    finally:
        if QCoreApplication.instance() is not None:
            # pytest-qt closes widgets and posts deleteLater() during teardown. Drain those
            # events before collecting Python cycles that might hold widget wrappers.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
