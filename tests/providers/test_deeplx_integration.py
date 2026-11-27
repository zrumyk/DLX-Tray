"""Exercise the DLX client and retry policy over loopback HTTP."""

from __future__ import annotations

from threading import Event

import httpx
import pytest

from dlx_tray.core.chunker import HARD_MAX_UNITS
from dlx_tray.core.models import (
    AuthError,
    Chunk,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.pipeline import FailureAction, JobResult, TranslationJob
from dlx_tray.core.retry import RetryPolicy, call_with_retry
from dlx_tray.providers import deeplx
from dlx_tray.providers.deeplx import DeeplxTranslator
from tests.fakes.fake_dlx import FakeDLXFactory


@pytest.mark.parametrize(
    ("step", "error", "backoff_bounds"),
    [
        ("429", RateLimitedError, (15.0, 15.0)),
        ("429-retry-after", RateLimitedError, (9.0, 9.0)),
        ("429-retry-after-15", RateLimitedError, (15.0, 15.0)),
        ("503", ProviderUnavailableError, (1.6, 2.4)),
        ("slow", ProviderUnavailableError, (1.6, 2.4)),
        ("drop", ProviderUnavailableError, (1.6, 2.4)),
    ],
)
def test_job_retries_only_the_failed_chunk(
    fake_dlx: FakeDLXFactory,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    error: type[TranslationError],
    backoff_bounds: tuple[float, float],
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    waits: list[float] = []
    retries: list[tuple[int, float, type[TranslationError]]] = []
    progress: list[tuple[int, int, str]] = []

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += seconds
        waits.append(seconds)
        return not cancel.is_set()

    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        retries.append((attempt, seconds, type(exc)))

    def on_failure(chunk: Chunk, exc: TranslationError) -> FailureAction:
        pytest.fail("The transient error should recover before a failure decision is needed")

    def on_progress(index: int, total: int, text: str) -> None:
        progress.append((index, total, text))

    with (
        fake_dlx(["ok", step, "reverse"]) as server,
        DeeplxTranslator(
            server.url, server.token, timeout=0.5 if step == "slow" else 3
        ) as translator,
    ):
        job = TranslationJob(
            translator=translator,
            max_units=5,
            sleep=sleep,
        )
        result = job.run(
            "Hello\r\nWorld",
            Language.AUTO,
            Language.UK,
            on_retry=on_retry,
            on_failure=on_failure,
            on_progress=on_progress,
        )

    assert len(waits) == 1
    assert backoff_bounds[0] <= waits[0] <= backoff_bounds[1]
    assert retries == [(1, waits[0], error)]
    assert progress == [(1, 2, "[UK] Hello\r\n"), (2, 2, "[UK] Hello\r\n[UK] dlroW")]
    assert result == JobResult(
        "[UK] Hello\r\n[UK] dlroW", Language.EN, chunk_providers=("dlx", "dlx")
    )


@pytest.mark.parametrize(
    ("step", "error", "delay"),
    [
        ("429", RateLimitedError, 15.0),
        ("429-retry-after", RateLimitedError, 9.0),
        ("503", ProviderUnavailableError, 2.0),
        ("drop", ProviderUnavailableError, 2.0),
    ],
)
def test_retries_scripted_failure(
    fake_dlx: FakeDLXFactory,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    error: type[TranslationError],
    delay: float,
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    waits: list[float] = []
    failures: list[tuple[int, float, type[TranslationError]]] = []

    def sleep(seconds: float, cancel: Event) -> bool:
        nonlocal now
        now += seconds
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


@pytest.mark.parametrize(
    ("step", "error"), [("401", AuthError), ("403", AuthError), ("413", TextTooLongError)]
)
def test_job_does_not_retry_scripted_client_errors(
    fake_dlx: FakeDLXFactory, step: str, error: type[TranslationError]
) -> None:
    def on_retry(attempt: int, seconds: float, exc: TranslationError) -> None:
        pytest.fail(f"Unexpected retry after {exc}")

    with fake_dlx([step, "reverse"]) as server, DeeplxTranslator(server.url, server.token) as tr:
        result = TranslationJob(translator=tr).run(
            "Hello", Language.EN, Language.UK, on_retry=on_retry
        )
        assert isinstance(result.error, error)
        assert server.next_step() == "reverse"


def test_batch_at_limit_with_blank_entry(fake_dlx: FakeDLXFactory) -> None:
    text = "a" * HARD_MAX_UNITS
    blank = " " * (HARD_MAX_UNITS + 1)
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        result = translator.translate([text, blank], Language.EN, Language.UK)
    assert result == [
        TranslationResult(f"[UK] {text}", Language.EN, "dlx"),
        TranslationResult(blank, Language.EN, "dlx"),
    ]


def test_cooldown_doubles_across_jobs_and_resets_on_success(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(deeplx, "monotonic", lambda: now)
    with (
        fake_dlx(["429", "429", "ok", "429"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        job = TranslationJob(translator=translator, retry_policy=RetryPolicy(max_attempts=1))

        def run() -> JobResult:
            return job.run("Hello", Language.EN, Language.UK)

        def expect_cooldown(seconds: float) -> None:
            error = run().error
            assert isinstance(error, RateLimitedError)
            assert error.retry_after == seconds

        expect_cooldown(15.0)
        now += 5.0
        expect_cooldown(10.0)
        now += 10.0
        expect_cooldown(30.0)
        now += 30.0
        assert run().text == "[UK] Hello"
        expect_cooldown(15.0)


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
