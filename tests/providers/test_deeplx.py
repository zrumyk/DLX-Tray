"""Tests for the DLX client, with HTTP mocked by respx."""

import json
import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from dlx_tray.core.chunker import HARD_MAX_UNITS
from dlx_tray.core.diagnostics import DiagnosticTimeline, ProviderAttempt
from dlx_tray.core.models import (
    DlxAuthError,
    DlxForbiddenError,
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


@pytest.mark.parametrize(
    ("header", "expected"),
    [(" HIT ", "hit"), ("miss", "miss"), ("synthetic-sensitive-header", None), (None, None)],
)
def test_attempt_records_only_known_cache_values(
    mock: respx.MockRouter, header: str | None, expected: str | None
) -> None:
    diagnostics = DiagnosticTimeline()
    response = ok("result")
    if header is not None:
        response.headers["X-DLX-Cache"] = header
    mock.post(URL).mock(return_value=response)

    with DeeplxTranslator(BASE, TOKEN, diagnostics=diagnostics) as translator:
        assert translate(translator, "sample") == ["result"]

    (attempt,) = diagnostics.snapshot().events
    assert isinstance(attempt, ProviderAttempt)
    assert attempt.provider == "dlx"
    assert attempt.status == 200
    assert attempt.elapsed >= 0
    assert attempt.cache == expected


def test_omits_authorization_when_no_token_is_configured(mock: respx.MockRouter) -> None:
    route = mock.post(URL).mock(return_value=ok("Привіт"))
    with DeeplxTranslator(BASE, "") as translator:
        assert translate(translator, "Hello") == ["Привіт"]
    assert "Authorization" not in route.calls.last.request.headers


def test_appends_endpoint_to_base_path_before_query(mock: respx.MockRouter) -> None:
    route = mock.post(f"{BASE}/proxy%2Fdlx/translate?source=dev").mock(return_value=ok("Привіт"))
    with DeeplxTranslator(f"{BASE}/proxy%2Fdlx/?source=dev#setup", TOKEN) as translator:
        assert translate(translator, "Hello") == ["Привіт"]
    assert route.calls.last.request.url.raw_path == b"/proxy%2Fdlx/translate?source=dev"
    assert not route.calls.last.request.url.fragment


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


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, DlxAuthError), (403, DlxForbiddenError)],
)
def test_access_errors_do_not_trust_the_response_body(
    mock: respx.MockRouter, dlx: DeeplxTranslator, status: int, error: type[TranslationError]
) -> None:
    body = {"code": status, "message": "Invalid access token"}
    mock.post(URL).mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(error, match=f"HTTP {status}") as info:
        translate(dlx, "Hello")
    assert body["message"] not in str(info.value)


def test_logging_excludes_input_and_server_supplied_details(
    mock: respx.MockRouter, dlx: DeeplxTranslator, caplog: pytest.LogCaptureFixture
) -> None:
    source_text = "private source text"
    route = mock.post(URL)
    route.side_effect = [
        ok("private translation", source_lang=TOKEN),
        httpx.Response(401, json={"message": f"{source_text} {TOKEN}"}),
    ]
    with caplog.at_level(logging.DEBUG, logger="dlx_tray.providers.deeplx"):
        translate(dlx, source_text)
        with pytest.raises(DlxAuthError):
            translate(dlx, source_text)

    assert "unknown source language" in caplog.text
    assert "DLX answered HTTP 401" in caplog.text
    assert TOKEN not in caplog.text
    assert source_text not in caplog.text
    assert "private translation" not in caplog.text


def test_413_is_text_too_long(mock: respx.MockRouter, dlx: DeeplxTranslator) -> None:
    message = "text exceeds maximum length: 1501 characters (anonymous oneshot limit is 1500)"
    mock.post(URL).mock(return_value=httpx.Response(413, json={"code": 413, "message": message}))
    with pytest.raises(TextTooLongError, match="exceeds maximum length"):
        translate(dlx, "Hello")


@pytest.mark.parametrize(("headers", "retry_after"), [({}, 15.0), ({"Retry-After": "12"}, 12.0)])
def test_429_is_rate_limited(
    mock: respx.MockRouter,
    dlx: DeeplxTranslator,
    headers: dict[str, str],
    retry_after: float | None,
) -> None:
    body = {"code": 429, "message": "too many requests, your IP has been blocked by DeepL"}
    mock.post(URL).mock(return_value=httpx.Response(429, json=body, headers=headers))
    with pytest.raises(RateLimitedError, match="rate-limited") as info:
        translate(dlx, "Hello")
    assert body["message"] not in str(info.value)
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


@pytest.mark.parametrize(
    "batch",
    [
        ["\U0001f600" * (HARD_MAX_UNITS // 2) + "a"],
        ["\U0001f600" * (HARD_MAX_UNITS // 4)] * 2 + ["a"],
        ["\x1f" * (HARD_MAX_UNITS + 1)],
        [" " * HARD_MAX_UNITS + "a"],
    ],
)
def test_oversized_batch_sends_nothing(
    mock: respx.MockRouter, dlx: DeeplxTranslator, batch: list[str]
) -> None:
    route = mock.post(URL).mock(return_value=ok(batch))
    with pytest.raises(TextTooLongError, match="1501 UTF-16 units"):
        dlx.translate(batch, Language.AUTO, Language.UK)
    assert not route.called


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


def test_owned_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    with DeeplxTranslator(BASE, TOKEN) as translator:
        client = translator._client  # pyright: ignore[reportPrivateUsage]
        assert client.trust_env is False
        assert not client._mounts  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("url", ["127.0.0.1:1188", "ftp://127.0.0.1", ""])
def test_rejects_url_without_http_scheme(url: str) -> None:
    with pytest.raises(ValueError, match="http://"):
        DeeplxTranslator(url, TOKEN)


def test_413_logged_once_as_error(
    mock: respx.MockRouter, dlx: DeeplxTranslator, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="dlx_tray")
    mock.post(URL).mock(return_value=httpx.Response(413, json={"message": "too long"}))
    with pytest.raises(TextTooLongError):
        translate(dlx, "Hello")
    assert [r.levelname for r in caplog.records if "413" in r.getMessage()] == ["ERROR"]
