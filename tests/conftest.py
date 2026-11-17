"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.txt"))


def read_fixture(name: str) -> str:
    """A fixture's exact text. Bytes are decoded without newline translation."""
    return (FIXTURES_DIR / name).read_bytes().decode("utf-8")


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """``fixture_text("uk_article.txt")`` returns the file's exact contents."""
    return read_fixture
