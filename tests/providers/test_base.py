"""Tests for the Translator protocol helpers."""

from datetime import UTC, datetime

import pytest

from dlx_tray.providers.base import Translator, parse_retry_after
from dlx_tray.providers.deeplx import DeeplxTranslator

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("soon", None),
        ("nan", None),
        ("0", 0.0),
        ("7", 7.0),
        (" 2.5 ", 2.5),
        ("-3", 0.0),
        ("Tue, 17 Nov 2026 12:00:30 GMT", 30.0),
        ("Tue, 17 Nov 2026 11:59:00 GMT", 0.0),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == expected


def test_parse_retry_after_naive_date_is_utc() -> None:
    assert parse_retry_after("Tue, 17 Nov 2026 12:01:00", now=NOW) == 60.0


def test_parse_retry_after_defaults_to_the_real_clock() -> None:
    assert parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0


def test_deeplx_translator_satisfies_protocol() -> None:
    with DeeplxTranslator("http://127.0.0.1:1", "t") as translator:
        assert isinstance(translator, Translator)
