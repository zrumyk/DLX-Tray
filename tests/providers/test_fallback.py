"""Exercise per-chunk failover and rate-limited primary recovery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event, Lock
from unittest.mock import Mock

import pytest
import requests

from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TranslationResult,
)
from dlx_tray.core.pipeline import JobResult, TranslationJob
from dlx_tray.core.retry import RequestDispatch, RetryPolicy
from dlx_tray.providers import deeplx, fallback
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deepl_official import DeepLOfficialTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import FallbackTranslator
from tests.fakes.fake_dlx import FakeDLXFactory


def test_parallel_fallback_serializes_calls_to_the_official_sdk(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, sent, second_waiting, second_sent, release = (Event() for _ in range(5))
    guard = Lock()
    sdk_starts: list[int] = []
    original_start = DeepLOfficialTranslator._start

    def start(translator: DeepLOfficialTranslator, dispatch: RequestDispatch) -> None:
        with guard:
            second = entered.is_set()
            entered.set()
        if second:
            assert sent.wait(5)
            second_waiting.set()
        original_start(translator, dispatch)

    def send(*_args: object, **_kwargs: object) -> requests.Response:
        with guard:
            sdk_starts.append(len(sdk_starts) + 1)
            (second_sent if sent.is_set() else sent).set()
        assert release.wait(5)
        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'{"translations": [{"text": "Translated", "detected_source_language": "EN"}]}'
        )
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(DeepLOfficialTranslator, "_start", start)
    with (
        fake_dlx(["503", "503"]) as server,
        DeeplxTranslator(server.url, server.token) as primary,
        closing(DeepLOfficialTranslator("fixture:fx")) as secondary,
        ThreadPoolExecutor() as executor,
    ):
        job = TranslationJob(
            translator=FallbackTranslator(primary, secondary), concurrency=2, max_units=751
        )
        source = f"{'A' * 751}\n{'B' * 751}"
        result = executor.submit(job.run, source, Language.EN, Language.UK)
        try:
            assert sent.wait(5)
            assert second_waiting.wait(5)
            assert not second_sent.wait(0.1)
        finally:
            release.set()
        assert result.result(5).chunk_providers == ("deepl", "deepl")
    assert sdk_starts == [1, 2]


def test_fallback_cancellation_at_provider_notification_sends_no_http(
    fake_dlx: FakeDLXFactory,
) -> None:
    cancel = Event()
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    with fake_dlx(["reverse"]) as server, DeeplxTranslator(server.url, server.token) as primary:
        job = TranslationJob(translator=FallbackTranslator(primary, secondary), concurrency=2)
        result = job.run(
            "First", Language.EN, Language.UK, cancel=cancel, on_provider=lambda _name: cancel.set()
        )
        assert server.next_step() == "reverse"
    assert result.cancelled and result.text == ""
    secondary.translate.assert_not_called()


def test_connection_loss_falls_back_for_the_failed_chunk(fake_dlx: FakeDLXFactory) -> None:
    first, failed, last = (word.ljust(751, word[-1]) for word in ("Hello", "World", "Again"))
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    secondary.translate.return_value = [TranslationResult("[DeepL] World", Language.EN, "deepl")]
    with (
        fake_dlx(["ok", "drop", "ok"]) as server,
        DeeplxTranslator(server.url, server.token) as primary,
    ):
        job = TranslationJob(
            translator=FallbackTranslator(primary, secondary), max_units=751, request_delay=0
        )
        providers: list[str] = []
        rate_limits: list[str] = []
        result = job.run(
            f"{first}\r\n{failed}\r\n{last}",
            Language.AUTO,
            Language.UK,
            Formality.MORE,
            on_provider=providers.append,
            on_rate_limit=rate_limits.append,
        )

    assert providers == ["dlx", "dlx", "deepl", "dlx"]
    assert rate_limits == []
    assert result == JobResult(
        f"[UK] {first}\r\n[DeepL] World\r\n[UK] {last}",
        Language.EN,
        chunk_providers=("dlx", "deepl", "dlx"),
    )
    secondary.translate.assert_called_once_with(
        [failed], Language.AUTO, Language.UK, Formality.MORE
    )


@pytest.mark.parametrize(("status", "pause"), [("429", 15.0), ("429-retry-after", 9.0)])
def test_rate_limit_cooldown_across_jobs(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch, status: str, pause: float
) -> None:
    time = 100.0
    monkeypatch.setattr(fallback, "monotonic", lambda: time)
    monkeypatch.setattr(deeplx, "monotonic", lambda: time)
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    secondary.translate.return_value = [TranslationResult("Світ", Language.EN, "deepl")]
    with fake_dlx([status, "ok"]) as server, DeeplxTranslator(server.url, server.token) as primary:
        job = TranslationJob(translator=FallbackTranslator(primary, secondary))

        def run() -> JobResult:
            return job.run(
                "World",
                Language.AUTO,
                Language.UK,
                on_provider=providers.append,
                on_rate_limit=rate_limits.append,
            )

        providers: list[str] = []
        rate_limits: list[str] = []
        assert run().chunk_providers == ("deepl",)
        time += pause - 0.1
        assert run().chunk_providers == ("deepl",)
        time = 100.0 + pause
        assert run().chunk_providers == ("dlx",)

    assert providers == ["dlx", "deepl", "deepl", "dlx"]
    assert rate_limits == ["dlx", "dlx"]
    assert secondary.translate.call_count == 2


def test_empty_and_terminal_primary_failure_do_not_use_fallback() -> None:
    primary = Mock(spec=Translator)
    secondary = Mock(spec=Translator)
    translator = FallbackTranslator(primary, secondary)

    assert translator.translate([], Language.AUTO, Language.UK) == []
    primary.translate.assert_not_called()
    primary.translate.side_effect = AuthError("rejected")
    with pytest.raises(AuthError, match="rejected"):
        translator.translate(["Hello"], Language.AUTO, Language.UK)
    secondary.translate.assert_not_called()


def test_secondary_failure_uses_job_retry_budget_and_preserves_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fallback, "monotonic", lambda: 100.0)
    primary = Mock(spec=Translator)
    primary.name = "dlx"
    primary.translate.side_effect = [
        [TranslationResult("[UK] Hello", Language.EN, "dlx")],
        RateLimitedError("rate limited"),
    ]
    error = ProviderUnavailableError("official API unavailable")
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    secondary.translate.side_effect = error
    sleep = Mock(return_value=True)
    job = TranslationJob(
        translator=FallbackTranslator(primary, secondary),
        max_units=5,
        request_delay=0,
        retry_policy=RetryPolicy(max_attempts=2, jitter=0),
        sleep=sleep,
    )

    result = job.run("Hello World", Language.AUTO, Language.UK)

    assert result == JobResult("[UK] Hello ", Language.EN, error=error, chunk_providers=("dlx",))
    assert primary.translate.call_count == 2
    assert secondary.translate.call_count == 2
    sleep.assert_called_once()
    assert sleep.call_args.args[0] == 2.0
