"""Exercise the Cloud AI client against its HTTP contract."""

import json
from contextlib import closing
from threading import Event

import httpx
import pytest
import respx

from dlx_tray.contracts.cloud_v1 import ExtractTermsRequest
from dlx_tray.core.diagnostics import DiagnosticTimeline, ProviderAttempt
from dlx_tray.core.models import (
    AnalysisTier,
    CloudAuthError,
    CloudDeepTierForbiddenError,
    CloudQuotaExceededError,
    ContextBlock,
    GlossaryEntry,
    IndexedSegment,
    Language,
    NoteKind,
    PhilologicalNote,
    ProviderUnavailableError,
    RateLimitedError,
    StylisticRegister,
    TextTooLongError,
    TranslationCancelledError,
    TranslationError,
    TranslationOptions,
)
from dlx_tray.core.retry import RequestDispatch, event_sleep
from dlx_tray.providers.cloud_ai import CloudAiTranslator

URL = "https://cloud.example/run"
SECRET = "private-fixture-token"
BLOCK = ContextBlock((IndexedSegment(7, "private source"), IndexedSegment(3, "more source")))


def _dispatch(cancel: Event | None = None) -> RequestDispatch:
    return RequestDispatch(cancel or Event(), 0, 1, event_sleep)


def test_batch_preserves_indexes_and_sends_notes_glossary_and_context(
    respx_mock: respx.MockRouter,
) -> None:
    response = {
        "segments": [
            {"index": 3, "text": "Translated second", "cached": True},
            {
                "index": 7,
                "text": "Translated first",
                "detected_source": "EN",
                "notes": [{"kind": "grammar", "text": "Illustrative note"}],
            },
        ]
    }
    route = respx_mock.post(f"{URL}/v1/translate").respond(
        200, json=response, headers={"X-Cloud-Cache": "mixed"}
    )
    diagnostics = DiagnosticTimeline()
    block = ContextBlock(BLOCK.segments, ("previous paragraph",))
    options = TranslationOptions(
        analysis_tier=AnalysisTier.DEEP,
        register=StylisticRegister.LITERARY,
        style_context="Book chapter",
        glossary=(GlossaryEntry("private source", "private target", 3),),
        include_notes=True,
    )
    with closing(CloudAiTranslator(URL, SECRET, diagnostics=diagnostics)) as client:
        results = client.translate_block(block, Language.AUTO, Language.UK, options, _dispatch())
    assert [item.index for item in results] == [7, 3]
    assert results[0].result.text == "Translated first"
    assert results[0].result.notes == (PhilologicalNote(NoteKind.GRAMMAR, "Illustrative note"),)
    assert results[0].result.detected_source is Language.EN
    assert results[1].result.provider == "cloud_ai"
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert json.loads(request.content) == {
        "source_lang": "auto",
        "target_lang": "UK",
        "segments": [{"index": 7, "text": "private source"}, {"index": 3, "text": "more source"}],
        "context_before": ["previous paragraph"],
        "register": "literary",
        "style_context": "Book chapter",
        "glossary": [
            {"source_term": "private source", "target_term": "private target", "priority": 3}
        ],
        "include_notes": True,
        "analysis_tier": "deep",
    }
    (event,) = diagnostics.snapshot().events
    assert isinstance(event, ProviderAttempt)
    assert (event.status, event.cache, event.analysis_tier) == (200, "mixed", AnalysisTier.DEEP)


def test_check_token_and_extract_terms(respx_mock: respx.MockRouter) -> None:
    me = respx_mock.get(f"{URL}/v1/me").respond(
        200,
        json={
            "label": "tester",
            "allow_deep_tier": True,
            "daily_chars_remaining": 900,
            "monthly_chars_remaining": 5000,
        },
    )
    terms = respx_mock.post(f"{URL}/v1/extract-terms").respond(
        200,
        json={
            "terms": [
                {
                    "source_term": "source",
                    "target_term": "target",
                    "category": "phrase",
                    "count": 2,
                    "note": "note",
                }
            ]
        },
    )
    with closing(CloudAiTranslator(URL, SECRET)) as client:
        assert client.check_token().daily_chars_remaining == 900
        assert (
            client.extract_terms(ExtractTermsRequest(target_lang=Language.UK, segments=["source"]))
            .terms[0]
            .count
            == 2
        )
    assert all(
        route.calls.last.request.headers["Authorization"] == f"Bearer {SECRET}"
        for route in (me, terms)
    )
    assert json.loads(terms.calls.last.request.content)["segments"] == ["source"]


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "invalid_token", CloudAuthError),
        (401, "token_revoked", CloudAuthError),
        (403, "deep_tier_forbidden", CloudDeepTierForbiddenError),
        (403, None, CloudAuthError),
        (413, "payload_too_large", TextTooLongError),
        (429, "rate_limited", RateLimitedError),
        (429, "quota_exceeded", CloudQuotaExceededError),
        (503, "upstream_unavailable", ProviderUnavailableError),
        (422, None, TranslationError),
    ],
)
def test_errors_and_diagnostics_redact_untrusted_values(
    respx_mock: respx.MockRouter,
    status: int,
    code: str | None,
    expected: type[TranslationError],
) -> None:
    proxy_echo = f"{SECRET} private source private target"
    respx_mock.post(f"{URL}/v1/translate").respond(
        status,
        json={"code": code, "message": proxy_echo},
        headers={"Retry-After": "11", "X-Cloud-Cache": proxy_echo},
    )
    diagnostics = DiagnosticTimeline()
    with (
        closing(CloudAiTranslator(URL, SECRET, diagnostics=diagnostics)) as client,
        pytest.raises(expected) as error,
    ):
        client.translate_block(BLOCK, Language.AUTO, Language.UK, TranslationOptions(), _dispatch())
    assert type(error.value) is expected
    if isinstance(error.value, RateLimitedError):
        assert error.value.retry_after == 11
    (event,) = diagnostics.snapshot().events
    assert isinstance(event, ProviderAttempt)
    assert (event.status, event.retry_after, event.cache) == (status, 11, None)
    assert all(
        secret not in f"{error.value!r} {event!r}"
        for secret in (SECRET, "private source", "private target")
    )


@pytest.mark.parametrize("phase", ["before", "during"])
def test_cancellation(respx_mock: respx.MockRouter, phase: str) -> None:
    cancel = Event()

    def respond(_request: httpx.Request) -> httpx.Response:
        cancel.set()
        return httpx.Response(503)

    route = respx_mock.get(f"{URL}/v1/me").mock(side_effect=respond)
    if phase == "before":
        cancel.set()
    with (
        closing(CloudAiTranslator(URL, SECRET)) as client,
        pytest.raises(TranslationCancelledError),
    ):
        client.check_token(cancel=cancel)
    assert route.call_count == int(phase == "during")


@pytest.mark.parametrize("indices", [None, [7, 7], [7], [7, 99]])
def test_invalid_responses_fail_the_entire_batch(
    respx_mock: respx.MockRouter,
    indices: list[int] | None,
) -> None:
    route = respx_mock.post(f"{URL}/v1/translate")
    if indices is None:
        route.mock(side_effect=httpx.ConnectError(f"echoed {SECRET}"))
    else:
        route.respond(200, json={"segments": [{"index": i, "text": "x"} for i in indices]})
    with (
        closing(CloudAiTranslator(URL, SECRET)) as client,
        pytest.raises(ProviderUnavailableError) as error,
    ):
        client.translate_block(BLOCK, Language.AUTO, Language.UK, TranslationOptions(), _dispatch())
    assert SECRET not in str(error.value)
