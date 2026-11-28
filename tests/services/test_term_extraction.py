"""Exercise UTF-16 budget and cancellation of document sampling."""

from threading import Event

import pytest
from pydantic import ValidationError

from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import Language, TranslationCancelledError
from dlx_tray.services.term_extraction import extraction_request


def test_extracts_unique_passages_within_utf16_budget() -> None:
    request = extraction_request(
        [" ", " tide ", "tide", "😀😀😀 tail", "beyond the budget"],
        Language.EN,
        Language.UK,
        budget=10,
        cancel=Event(),
    )
    assert request.segments == ["tide", "😀😀😀"]
    assert sum(map(utf16_len, request.segments)) == 10


def test_extracts_only_the_available_prefix() -> None:
    request = extraction_request(
        ["1234567", "word longer than the remaining budget", "extra"],
        Language.AUTO,
        Language.UK,
        budget=10,
        cancel=Event(),
    )
    assert request.segments == ["1234567", "wor"]


def test_extraction_rejects_empty_samples_and_invalid_budgets() -> None:
    with pytest.raises(ValidationError):
        extraction_request(["", " "], Language.EN, Language.UK, cancel=Event())
    with pytest.raises(ValueError, match="budget"):
        extraction_request(["text"], Language.EN, Language.UK, budget=1, cancel=Event())


def test_extraction_cancels_before_building_request() -> None:
    cancel = Event()
    cancel.set()
    with pytest.raises(TranslationCancelledError):
        extraction_request(["text"], Language.EN, Language.UK, cancel=cancel)
