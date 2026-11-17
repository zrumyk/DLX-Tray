"""The Translator protocol shared by every backend, plus small HTTP helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable

from dlx_tray.core.models import Formality, Language, TranslationResult


@runtime_checkable
class Translator(Protocol):
    """A translation backend.

    ``translate`` returns one result per input text, in the same order, or raises a
    ``TranslationError`` subclass from ``core.models``. An empty ``texts`` returns ``[]``
    without a request. Backends that cannot honour ``formality`` ignore it.
    """

    @property
    def name(self) -> str: ...

    def translate(
        self,
        texts: Sequence[str],
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
    ) -> list[TranslationResult]: ...


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from an HTTP ``Retry-After`` header (delta-seconds or an HTTP-date).

    Returns None for a missing or unparsable value (including negative, fractional or absurdly
    large numbers); a date in the past gives 0.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():  # RFC 9110 delta-seconds: a non-negative integer
        try:
            return float(int(value))
        except (OverflowError, ValueError):  # past float range / past int's digit limit
            return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    return max((when - current).total_seconds(), 0.0)
