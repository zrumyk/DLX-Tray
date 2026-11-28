"""Exercise the official DeepL SDK through a mocked HTTP transport."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator


@pytest.fixture
def send(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    send = MagicMock()
    monkeypatch.setattr(requests.Session, "send", send)
    return send


def _response(status: int, data: object) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = json.dumps(data).encode("utf-8")
    return response


def test_key_check_uses_usage_without_submitting_text(send: MagicMock) -> None:
    send.return_value = _response(200, {"character_count": 0, "character_limit": 500000})
    translator = DeepLOfficialTranslator("test-key:fx", timeout=5.0)
    try:
        translator.check_key()
    finally:
        translator.close()

    send.assert_called_once()
    request = send.call_args.args[0]
    assert request.method == "GET"
    assert request.url == "https://api-free.deepl.com/v2/usage"
    assert request.headers["Authorization"] == "DeepL-Auth-Key test-key:fx"
    assert request.body is None
    assert send.call_args.kwargs["timeout"] == 5.0


def test_key_check_sanitizes_rejection(send: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    send.return_value = _response(403, {"message": "sensitive response"})
    translator = DeepLOfficialTranslator("test-key")
    try:
        with pytest.raises(AuthError, match="DeepL rejected the API key"):
            translator.check_key()
    finally:
        translator.close()

    assert "sensitive response" not in caplog.text
    assert "test-key" not in caplog.text


@pytest.mark.parametrize(
    ("key", "host"),
    [("test-key:fx", "api-free.deepl.com"), ("test-key", "api.deepl.com")],
)
def test_routes_by_key_and_pairs_results_in_order(send: MagicMock, key: str, host: str) -> None:
    send.return_value = _response(
        200,
        {
            "translations": [
                {"text": "Guten Tag", "detected_source_language": "en"},
                {"text": "Auf Wiedersehen", "detected_source_language": "itr"},
            ]
        },
    )
    translator = DeepLOfficialTranslator(f" {key} ")
    try:
        assert isinstance(translator, Translator)
        assert translator.translate(
            ("Hello", "Goodbye"), Language.AUTO, Language.DE, Formality.MORE
        ) == [
            TranslationResult("Guten Tag", Language.EN, "deepl"),
            TranslationResult("Auf Wiedersehen", None, "deepl"),
        ]
    finally:
        translator.close()

    send.assert_called_once()
    request = send.call_args.args[0]
    assert request.url == f"https://{host}/v2/translate"
    assert request.headers["Authorization"] == f"DeepL-Auth-Key {key}"
    assert send.call_args.kwargs["timeout"] == 30.0
    payload = json.loads(request.body)
    assert payload["text"] == ["Hello", "Goodbye"]
    assert "source_lang" not in payload
    assert payload["target_lang"] == "DE"
    assert payload["formality"] == "more"


def test_official_job_batches_file_segments_without_losing_alignment(send: MagicMock) -> None:
    send.return_value = _response(200, {"translations": [{"text": "Eins"}, {"text": "Zwei"}]})
    translator = DeepLOfficialTranslator("test-key")
    output: list[str] = []
    try:
        job = TranslationJob(translator=translator)
        job.run(
            "", Language.EN, Language.DE, file_segments=["One", "Two"], on_segment=output.append
        )
    finally:
        translator.close()
    assert output == ["Eins", "Zwei"]
    send.assert_called_once()
    assert json.loads(send.call_args.args[0].body)["text"] == ["One", "Two"]


@pytest.mark.parametrize("formality", list(Formality))
def test_sends_explicit_languages_and_formality(send: MagicMock, formality: Formality) -> None:
    send.return_value = _response(200, {"translations": [{"text": "Olá"}]})
    translator = DeepLOfficialTranslator("test-key", timeout=17)

    assert translator.translate(["Hello"], Language.EN, Language.PT_BR, formality) == [
        TranslationResult("Olá", None, "deepl")
    ]

    payload = json.loads(send.call_args.args[0].body)
    assert payload["source_lang"] == "EN"
    assert payload["target_lang"] == "PT-BR"
    assert payload["formality"] == formality.value
    assert send.call_args.kwargs["timeout"] == 17


def test_empty_batch_makes_no_request(send: MagicMock) -> None:
    assert DeepLOfficialTranslator("test-key").translate([], Language.AUTO, Language.UK) == []
    send.assert_not_called()


@pytest.mark.parametrize(
    ("source", "target"), [(Language.EN_US, Language.DE), (Language.DE, Language.AUTO)]
)
def test_invalid_language_direction(send: MagicMock, source: Language, target: Language) -> None:
    with pytest.raises(ValueError, match="not a valid"):
        DeepLOfficialTranslator("test-key").translate(["Hallo"], source, target)
    send.assert_not_called()


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (400, TranslationError, "HTTP 400"),
        (401, AuthError, "rejected the API key"),
        (403, AuthError, "rejected the API key"),
        (413, TextTooLongError, "size limit"),
        (429, RateLimitedError, "rate limit"),
        (456, TranslationError, "quota exceeded"),
        (500, ProviderUnavailableError, "could not provide a translation"),
        (503, ProviderUnavailableError, "could not provide a translation"),
    ],
)
def test_maps_http_errors_without_sdk_retries_or_sensitive_details(
    send: MagicMock,
    caplog: pytest.LogCaptureFixture,
    status: int,
    error: type[TranslationError],
    message: str,
) -> None:
    send.return_value = _response(status, {"message": "private text and test-key"})
    with pytest.raises(error, match=message) as raised:
        DeepLOfficialTranslator("test-key").translate(["private text"], Language.AUTO, Language.UK)

    assert type(raised.value) is error
    assert raised.value.__suppress_context__
    assert "private text" not in str(raised.value) + caplog.text
    assert "test-key" not in str(raised.value) + caplog.text
    send.assert_called_once()


def test_timeout_is_unavailable(send: MagicMock) -> None:
    send.side_effect = requests.exceptions.ReadTimeout()
    with pytest.raises(ProviderUnavailableError):
        DeepLOfficialTranslator("test-key").translate(["Hello"], Language.AUTO, Language.UK)
    send.assert_called_once()


@pytest.mark.parametrize("error", [requests.exceptions.SSLError, requests.exceptions.ProxyError])
def test_permanent_connection_failure_is_not_retryable(
    send: MagicMock, error: type[requests.exceptions.ConnectionError]
) -> None:
    send.side_effect = error("private proxy credentials")
    with pytest.raises(TranslationError, match="Check the network and proxy settings") as raised:
        DeepLOfficialTranslator("test-key").translate(["Hello"], Language.AUTO, Language.UK)

    assert type(raised.value) is TranslationError
    assert "private proxy credentials" not in str(raised.value)
    send.assert_called_once()


@pytest.mark.parametrize(
    "translations",
    [[], [{}], [{}, {}], {"wrong": "shape"}, [{"text": 42}]],
)
def test_rejects_invalid_responses(send: MagicMock, translations: object) -> None:
    send.return_value = _response(200, {"translations": translations})
    with pytest.raises(ProviderUnavailableError):
        DeepLOfficialTranslator("test-key").translate(["Hello"], Language.AUTO, Language.UK)
