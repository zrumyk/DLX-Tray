"""Exercise per-chunk failover and rate-limited primary recovery."""

from unittest.mock import Mock

import pytest

from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TranslationResult,
)
from dlx_tray.core.pipeline import JobResult, TranslationJob
from dlx_tray.core.retry import RetryPolicy
from dlx_tray.providers import fallback
from dlx_tray.providers.base import Translator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import FallbackTranslator
from tests.fakes.fake_dlx import FakeDLXFactory


def test_connection_loss_falls_back_for_the_failed_chunk(fake_dlx: FakeDLXFactory) -> None:
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    secondary.translate.return_value = [TranslationResult("[DeepL] World", Language.EN, "deepl")]
    with (
        fake_dlx(["ok", "drop", "ok"]) as server,
        DeeplxTranslator(server.url, server.token) as primary,
    ):
        job = TranslationJob(
            translator=FallbackTranslator(primary, secondary), max_units=5, request_delay=0
        )
        result = job.run("Hello\r\nWorld\r\nAgain", Language.AUTO, Language.UK, Formality.MORE)

    assert result == JobResult(
        "[UK] Hello\r\n[DeepL] World\r\n[UK] Again",
        Language.EN,
        chunk_providers=("dlx", "deepl", "dlx"),
    )
    secondary.translate.assert_called_once_with(
        ["World"], Language.AUTO, Language.UK, Formality.MORE
    )


@pytest.mark.parametrize(("status", "pause"), [("429", 5.0), ("429-retry-after", 9.0)])
def test_rate_limit_cooldown_across_jobs(
    fake_dlx: FakeDLXFactory, monkeypatch: pytest.MonkeyPatch, status: str, pause: float
) -> None:
    time = 100.0
    monkeypatch.setattr(fallback, "monotonic", lambda: time)
    secondary = Mock(spec=Translator)
    secondary.name = "deepl"
    secondary.translate.return_value = [TranslationResult("Світ", Language.EN, "deepl")]
    with fake_dlx([status, "ok"]) as server, DeeplxTranslator(server.url, server.token) as primary:
        job = TranslationJob(translator=FallbackTranslator(primary, secondary))

        assert job.run("World", Language.AUTO, Language.UK).chunk_providers == ("deepl",)
        time += pause - 0.1
        assert job.run("World", Language.AUTO, Language.UK).chunk_providers == ("deepl",)
        time = 100.0 + pause
        assert job.run("World", Language.AUTO, Language.UK).chunk_providers == ("dlx",)

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
    assert sleep.call_args.args[0] == 5.0
