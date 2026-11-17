"""Tests for the DLX client, with HTTP mocked by respx."""

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from dlx_tray.core.chunker import HARD_MAX_UNITS
from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
)
from dlx_tray.providers.deeplx import DEFAULT_TIMEOUT, DeeplxTranslator

BASE = "http://127.0.0.1:41188"
URL = f"{BASE}/translate"
TOKEN = "secret-token"


def ok(data: object, source_lang: object = "EN") -> httpx.Response:
    body = {"code": 200, "data": data, "source_lang": source_lang, "target_lang": "UK", "id": 1}
    return httpx.Response(200, json=body)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def dlx() -> Iterator[DeeplxTranslator]:
    with DeeplxTranslator(BASE + "/", TOKEN) as translator:
        yield translator


def translate(dlx: DeeplxTranslator, *texts: str) -> list[str]:
    return [r.text for r in dlx.translate(texts, Language.AUTO, Language.UK)]


def test_sends_batch_with_token_and_languages(
    mock: respx.MockRouter, dlx: DeeplxTranslator, fixture_text: Callable[[str], str]
) -> None:
    texts = [fixture_text("en_article.txt")[:300], fixture_text("emoji.txt")[:200]]
    route = mock.post(URL).mock(return_value=ok(["перший", "другий"]))

    results = dlx.translate(texts, Language.EN, Language.PT_BR, Formality.MORE)

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "text": texts,
        "source_lang": "EN",
        "target_lang": "PT-BR",
    }
    assert [r.text for r in results] == ["перший", "другий"]
    assert all(r.provider == "dlx" == dlx.name for r in results)
    assert all(r.detected_source is Language.EN for r in results)


def test_auto_source_is_sent_lowercase(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    route = mock.post(URL).mock(return_value=ok(["Привіт"]))
    assert translate(dlx, "Hello") == ["Привіт"]
    assert json.loads(route.calls.last.request.content)["source_lang"] == "auto"


def test_uses_30_second_timeout(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    route = mock.post(URL).mock(return_value=ok(["x"]))
    translate(dlx, "Hello")
    assert DEFAULT_TIMEOUT == 30.0
    assert route.calls.last.request.extensions["timeout"]["read"] == 30.0


def test_string_data_accepted_for_single_text(
    mock: respx.MockRouter, dlx: DeeplxTranslator
) -> None:
    mock.post(URL).mock(return_value=ok("Привіт"))
    assert translate(dlx, "Hello") == ["Привіт"]


def test_empty_texts_send_nothing(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    route = mock.post(URL)
    assert dlx.translate([], Language.AUTO, Language.UK) == []
    assert not route.called


@pytest.mark.parametrize(
    ("source_lang", "expected"),
    [
        ("EN", Language.EN),
        ("uk", Language.UK),
        ("XX", None),
        ("", None),
        (None, None),
        ("auto", None),
        (42, None),
    ],
)
def test_detected_source(
    mock: respx.MockRouter, dlx: DeeplxTranslator, source_lang: object, expected: Language | None
) -> None:
    mock.post(URL).mock(return_value=ok(["x"], source_lang=source_lang))
    assert dlx.translate(["x"], Language.AUTO, Language.UK)[0].detected_source is expected


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(mock: respx.MockRouter, dlx: DeeplxTranslator, status: int) -> None:
    body = {"code": status, "message": "Invalid access token"}
    mock.post(URL).mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(AuthError, match="Invalid access token"):
        translate(dlx, "Hello")


def test_413_is_text_too_long(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    message = "text exceeds maximum length: 1501 characters (anonymous oneshot limit is 1500)"
    mock.post(URL).mock(return_value=httpx.Response(413, json={"code": 413, "message": message}))
    with pytest.raises(TextTooLongError, match="exceeds maximum length"):
        translate(dlx, "Hello")


@pytest.mark.parametrize(("headers", "retry_after"), [({}, None), ({"Retry-After": "12"}, 12.0)])
def test_429_is_rate_limited(
    mock: respx.MockRouter,
    dlx: DeeplxTranslator,
    headers: dict[str, str],
    retry_after: float | None,
) -> None:
    body = {"code": 429, "message": "too many requests, your IP has been blocked by DeepL"}
    mock.post(URL).mock(return_value=httpx.Response(429, json=body, headers=headers))
    with pytest.raises(RateLimitedError, match="too many requests") as info:
        translate(dlx, "Hello")
    assert info.value.retry_after == retry_after


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_unavailable(mock: respx.MockRouter, dlx: DeeplxTranslator, status: int) -> None:
    body = {"code": status, "message": "Translation failed"}
    mock.post(URL).mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(ProviderUnavailableError, match="Translation failed"):
        translate(dlx, "Hello")


def test_other_4xx_is_plain_translation_error(
    mock: respx.MockRouter, dlx: DeeplxTranslator
) -> None:
    mock.post(URL).mock(return_value=httpx.Response(400, text="bad request"))
    with pytest.raises(TranslationError, match="HTTP 400") as info:
        translate(dlx, "Hello")
    assert type(info.value) is TranslationError


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("dropped"),
    ],
)
def test_connection_errors_are_unavailable(
    mock: respx.MockRouter, dlx: DeeplxTranslator, exc: Exception
) -> None:
    mock.post(URL).mock(side_effect=exc)
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        translate(dlx, "Hello")


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t")])
def test_timeouts_are_unavailable(
    mock: respx.MockRouter, dlx: DeeplxTranslator, exc: Exception
) -> None:
    mock.post(URL).mock(side_effect=exc)
    with pytest.raises(ProviderUnavailableError, match="in time"):
        translate(dlx, "Hello")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a"]),
        httpx.Response(200, json={"code": 200}),
        httpx.Response(200, json={"code": 200, "data": [1]}),
        httpx.Response(200, json={"code": 200, "data": ["a", "b"]}),
    ],
)
def test_malformed_success_is_unavailable(
    mock: respx.MockRouter, dlx: DeeplxTranslator, response: httpx.Response
) -> None:
    mock.post(URL).mock(return_value=response)
    with pytest.raises(ProviderUnavailableError, match="DLX"):
        translate(dlx, "Hello")


def test_batch_over_limit_is_refused_locally(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    route = mock.post(URL)
    half = "\U0001f600" * (HARD_MAX_UNITS // 4)  # emoji cost 2 UTF-16 units each
    with pytest.raises(TextTooLongError, match="1502"):
        dlx.translate([half, half, "ab"], Language.AUTO, Language.UK)
    assert not route.called


def test_batch_at_limit_is_sent(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    mock.post(URL).mock(return_value=ok(["x"]))
    assert translate(dlx, "a" * HARD_MAX_UNITS) == ["x"]


@pytest.mark.parametrize(
    ("source", "target"), [(Language.AUTO, Language.AUTO), (Language.EN_GB, Language.UK)]
)
def test_invalid_languages_rejected(
    dlx: DeeplxTranslator, source: Language, target: Language
) -> None:
    with pytest.raises(ValueError, match="not a valid"):
        dlx.translate(["Hello"], source, target)


def test_token_and_text_never_logged(
    mock: respx.MockRouter, dlx: DeeplxTranslator, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="dlx_tray")
    mock.post(URL).mock(return_value=httpx.Response(503, json={"message": "Translation failed"}))
    with pytest.raises(ProviderUnavailableError):
        translate(dlx, "very private sentence")
    assert caplog.records
    assert TOKEN not in caplog.text
    assert "very private sentence" not in caplog.text


def test_shared_client_is_not_closed() -> None:
    client = httpx.Client()
    with DeeplxTranslator(BASE, TOKEN, client=client):
        pass
    assert not client.is_closed
    client.close()


def test_owned_client_is_closed() -> None:
    translator = DeeplxTranslator(BASE, TOKEN)
    translator.close()
    assert translator._client.is_closed  # pyright: ignore[reportPrivateUsage]


def test_error_without_message_falls_back_to_status(
    mock: respx.MockRouter, dlx: DeeplxTranslator
) -> None:
    mock.post(URL).mock(return_value=httpx.Response(503, json={"code": 503}))
    with pytest.raises(ProviderUnavailableError, match="HTTP 503"):
        translate(dlx, "Hello")
