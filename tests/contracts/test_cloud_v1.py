"""Cover Cloud AI v1 payload limits and round trips."""

from collections.abc import Mapping

import pytest
from pydantic import BaseModel, ValidationError

from dlx_tray.contracts.cloud_v1 import (
    ErrorResponse,
    ExtractTermsRequest,
    ExtractTermsResponse,
    MeResponse,
    TranslateRequest,
    TranslateResponse,
)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            MeResponse,
            dict(
                label="test",
                allow_deep_tier=True,
                daily_chars_remaining=0,
                monthly_chars_remaining=100,
            ),
        ),
        (
            TranslateRequest,
            dict(
                target_lang="UK",
                segments=[{"index": 3, "text": "Source"}],
                context_before=["Previous paragraph"],
                register="academic",
                analysis_tier="deep",
                glossary=[{"source_term": "Source", "target_term": "Джерело", "priority": 2}],
                include_notes=True,
            ),
        ),
        (
            TranslateResponse,
            dict(
                segments=[
                    dict(
                        index=3,
                        text="Джерело",
                        detected_source="EN",
                        cached=True,
                        notes=[{"kind": "alternative", "text": "Витік"}],
                    )
                ]
            ),
        ),
        (ExtractTermsRequest, dict(target_lang="UK", segments=["Source"])),
        (
            ExtractTermsResponse,
            dict(
                terms=[dict(source_term="Source", target_term="Джерело", category="term", count=2)]
            ),
        ),
        (ErrorResponse, dict(code="quota_exceeded", message="Quota exceeded")),
    ],
)
def test_cloud_model_round_trip(model: type[BaseModel], payload: Mapping[str, object]) -> None:
    instance = model.model_validate(payload)
    assert model.model_validate_json(instance.model_dump_json()) == instance
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(dict(payload) | {"unexpected": True})


@pytest.mark.parametrize(
    "changes",
    [
        {"target_lang": "auto"},
        {"source_lang": "EN-US"},
        {"register": "unknown"},
        {"segments": []},
        {"segments": [{"index": -1, "text": "Source"}]},
        {"segments": [{"index": "1", "text": "Source"}]},
        {"segments": [{"index": 0, "text": " "}]},
        {"segments": [{"index": 0, "text": "Source"}] * 2},
        {"segments": [{"index": i, "text": "Source"} for i in range(26)]},
        {"analysis_tier": "deep", "segments": [{"index": 0, "text": "x" * 12_001}]},
        {"segments": [{"index": 0, "text": "\U0001f600" * 10_001}]},
        {"context_before": ["\U0001f600" * 751]},
        {"context_before": ["a", "b", "c"]},
        {"style_context": "x" * 201},
    ],
)
def test_translate_request_rejects_invalid_payload(changes: dict[str, object]) -> None:
    payload = {"target_lang": "UK", "segments": [{"index": 2, "text": "Source"}]}
    with pytest.raises(ValidationError):
        TranslateRequest.model_validate(payload | changes)


def test_extraction_rejects_over_budget() -> None:
    with pytest.raises(ValidationError, match="UTF-16 budget"):
        ExtractTermsRequest.model_validate(
            {"target_lang": "UK", "segments": ["\U0001f600" * 30_001]}
        )


def test_translation_response_rejects_blank_segment() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        TranslateResponse.model_validate({"segments": [{"index": 0, "text": " "}]})
