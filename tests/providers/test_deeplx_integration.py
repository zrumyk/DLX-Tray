"""Exercise the DLX client and retry policy over loopback HTTP."""

from __future__ import annotations

from threading import Event

import httpx
import pytest

from dlx_tray.core.models import (
    AuthError,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.retry import RetryPolicy, call_with_retry
from dlx_tray.providers.deeplx import DeeplxTranslator
from tests.fakes.fake_dlx import FakeDLXFactory


@pytest.mark.parametrize(
    ("step", "error", "delay"),
    [
        ("429", RateLimitedError, 5.0),
        ("429-retry-after", RateLimitedError, 9.0),
        ("503", ProviderUnavailableError, 5.0),
        ("drop", ProviderUnavailableError, 5.0),
    ],
)
def test_retries_scripted_failure(
    fake_dlx: FakeDLXFactory, step: str, error: type[TranslationError], delay: float
) -> None:
    waits: list[float] = []
    failures: list[tuple[int, float, type[TranslationError]]] = []

    def sleep(seconds: float, cancel: Event) -> bool:
        waits.append(seconds)
        return not cancel.is_set()

    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        failures.append((attempt, seconds, type(exc)))

    with fake_dlx([step, "ok"]) as server, DeeplxTranslator(server.url, server.token) as translator:
        result = call_with_retry(
            lambda: translator.translate(["Hello", "   ", "World"], Language.AUTO, Language.UK),
            RetryPolicy(max_attempts=2, jitter=0),
            sleep=sleep,
            on_retry=on_retry,
        )

    assert waits == [delay]
    assert failures == [(1, delay, error)]
    assert result == [
        TranslationResult("[UK] Hello", Language.EN, "dlx"),
        TranslationResult("   ", Language.EN, "dlx"),
        TranslationResult("[UK] World", Language.EN, "dlx"),
    ]


@pytest.mark.parametrize(("step", "error"), [("401", AuthError), ("413", TextTooLongError)])
def test_scripted_client_errors(
    fake_dlx: FakeDLXFactory, step: str, error: type[TranslationError]
) -> None:
    with (
        fake_dlx([step]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
        pytest.raises(error),
    ):
        translator.translate(["Hello"], Language.EN, Language.UK)


def test_slow_step_times_out_without_blocking_next_request(fake_dlx: FakeDLXFactory) -> None:
    with fake_dlx(["slow", "ok"]) as server:
        with DeeplxTranslator(server.url, server.token, timeout=0.5) as translator:
            with pytest.raises(ProviderUnavailableError, match="did not answer in time") as exc:
                translator.translate(["Hello"], Language.EN, Language.UK)
            assert isinstance(exc.value.__cause__, httpx.ReadTimeout)
        with DeeplxTranslator(server.url, server.token, timeout=3) as translator:
            assert translator.translate(["Hello"], Language.EN, Language.UK)[0].text == "[UK] Hello"


def test_health_auth_and_payload_validation_leave_script_intact(fake_dlx: FakeDLXFactory) -> None:
    with fake_dlx(["reverse"]) as server, httpx.Client(trust_env=False, timeout=3) as client:
        endpoint = f"{server.url}/translate"
        payload = {"text": "Hello", "target_lang": "UK"}
        assert client.get(server.url).json()["code"] == 200
        assert client.post(endpoint, json=payload).status_code == 401
        assert client.post(endpoint, params={"token": "wrong"}, json=payload).status_code == 401

        auth = {"Authorization": f"DeepL-Auth-Key {server.token}"}
        too_long = {**payload, "text": ["a", "\U0001f600" * 750]}
        assert client.post(endpoint, headers=auth, json=too_long).status_code == 413
        assert client.post(endpoint, headers=auth, json={**payload, "text": ""}).status_code == 404

        response = client.post(endpoint, params={"token": server.token}, json=payload)
        assert response.status_code == 200
        assert response.json() == {
            "code": 200,
            "data": "[UK] olleH",
            "source_lang": "EN",
            "target_lang": "UK",
            "alternatives": None,
            "id": 123,
            "method": "Free",
        }
        assert client.post(endpoint, headers=auth, json=payload).json()["data"] == "[UK] Hello"
