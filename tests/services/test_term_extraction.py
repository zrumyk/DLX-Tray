"""Exercise UTF-16 budget and cancellation of document sampling."""

from threading import Event

import pytest
from pydantic import ValidationError

from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import Language, TranslationCancelledError
from dlx_tray.services.term_extraction import extraction_request


def test_extracts_unique_passages_within_utf16_budget() -> None:
    request = extraction_request(
        [" ", " tide ", "tide", "😀😀😀"],
        Language.EN,
        Language.UK,
        budget=10,
        cancel=Event(),
    )
    assert request.segments == ["tide", "😀😀😀"]
    assert sum(map(utf16_len, request.segments)) == 10


@pytest.mark.parametrize("single_segment", [False, True])
def test_samples_from_start_middle_and_end(single_segment: bool) -> None:
    segments = ["start!", "a" * 20, "middle", "b" * 20, "finish"]
    request = extraction_request(
        ["".join(segments)] if single_segment else segments,
        Language.AUTO,
        Language.UK,
        budget=18,
        cancel=Event(),
    )
    assert request.segments == ["start!", "middle", "finish"]


def test_sampling_drops_an_incomplete_surrogate_pair_at_the_boundary() -> None:
    request = extraction_request(
        ["a😀a" + "b" * 19 + "😀cd" + "e" * 19 + "z😀z"],
        Language.EN,
        Language.UK,
        budget=8,
        cancel=Event(),
    )
    assert sum(map(utf16_len, request.segments)) <= 8
    assert request.segments[0] == "a😀a"
    assert request.segments[-1] == "z"


def test_tiny_budget_keeps_one_complete_astral_character() -> None:
    request = extraction_request(["😀😀"], Language.EN, Language.UK, budget=2, cancel=Event())
    assert request.segments == ["😀"]


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
