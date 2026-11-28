"""Verify per-job routing and ordered fallback with Cloud AI configured."""

from collections.abc import Sequence
from threading import Event
from unittest.mock import Mock

import pytest
import respx

from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import (
    AnalysisTier,
    CloudAuthError,
    ContextBlock,
    GlossaryEntry,
    IndexedSegment,
    Language,
    ProviderUnavailableError,
    StylisticRegister,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.retry import RequestDispatch, RequestStartClock
from dlx_tray.core.router import HybridRouter
from dlx_tray.core.single_flight import SingleFlight
from dlx_tray.providers.base import BlockTranslator, Translator
from dlx_tray.providers.cloud_ai import CloudAiTranslator
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.providers.fallback import BlockFallbackTranslator, FallbackTranslator
from tests.fakes.fake_dlx import FakeDLXFactory


@pytest.mark.parametrize("starting", [False, True])
def test_modes_and_legacy_routing(starting: bool) -> None:
    dlx = Mock(spec=Translator, name="DLX")
    dlx.name = "dlx"
    official = Mock(spec=Translator)
    official.name = "deepl"
    cloud = Mock(spec=BlockTranslator)
    cloud.name = "cloud_ai"
    assert HybridRouter("cloud_only", dlx=dlx, official=official).select_route() is None
    assert HybridRouter("cloud_only", cloud=cloud, dlx=dlx).select_route() is cloud
    assert HybridRouter("dlx_only", dlx=dlx, cloud=cloud).select_route(file=True) is dlx
    assert (
        HybridRouter("official_only", official=official, cloud=cloud).select_route(file=True)
        is official
    )
    assert HybridRouter(dlx_starting=starting, official=official).select_route(file=True) is (
        None if starting else official
    )
    legacy = HybridRouter(dlx=dlx, official=official).select_route("A" * 2000, file=True)
    assert isinstance(legacy, FallbackTranslator)
    assert legacy.provider_names() == ("dlx", "deepl")


def test_cloud_first_requests_even_when_dlx_is_starting_or_unavailable() -> None:
    cloud = Mock(spec=BlockTranslator)
    cloud.name = "cloud_ai"
    official = Mock(spec=Translator)
    official.name = "deepl"
    for starting in (True, False):
        router = HybridRouter(threshold=4, cloud=cloud, official=official, dlx_starting=starting)
        assert isinstance(router.select_route("AB\U0001f600"), BlockFallbackTranslator)
        assert router.select_route("ABC\U0001f600") is cloud
        assert router.select_route(file=True) is cloud
        assert router.select_route(extract_terms=True) is cloud
        for options in (
            TranslationOptions(register=StylisticRegister.ACADEMIC),
            TranslationOptions(style_context="paper"),
            TranslationOptions(glossary=(GlossaryEntry("term", "target"),)),
            TranslationOptions(include_notes=True),
            TranslationOptions(analysis_tier=AnalysisTier.DEEP),
        ):
            assert router.select_route("text", options=options) is cloud
        assert isinstance(
            router.select_route(file=True, allow_plain_fallback=True), BlockFallbackTranslator
        )
        assert (
            router.select_route(
                file=True, options=TranslationOptions(include_notes=True), allow_plain_fallback=True
            )
            is cloud
        )


def test_short_job_skips_cloud_after_quota_and_preserves_dlx_cooldown(
    respx_mock: respx.MockRouter, fake_dlx: FakeDLXFactory
) -> None:
    cloud_url = "https://cloud.example"
    cloud_mock = respx_mock.post(f"{cloud_url}/v1/translate").respond(
        429, json={"code": "quota_exceeded", "message": "Exhausted"}
    )
    official = Mock(spec=Translator)
    official.name = "deepl"
    official.translate.return_value = [TranslationResult("Translated", Language.EN, "deepl")]
    cloud = CloudAiTranslator(cloud_url, "token")
    with (
        fake_dlx(["429", "reverse"]) as server,
        DeeplxTranslator(server.url, server.token) as dlx,
    ):
        respx_mock.post(f"{server.url}/translate").pass_through()
        route = HybridRouter(dlx=dlx, cloud=cloud, official=official).select_route("One\n\nTwo")
        assert isinstance(route, FallbackTranslator)
        assert route.provider_names() == ("dlx", "cloud_ai", "deepl")
        job = TranslationJob(translator=route, flights=SingleFlight())
        attempts: list[str] = []
        result = job.run("One\n\nTwo", Language.EN, Language.UK, on_provider=attempts.append)
        assert server.next_step() == "reverse"
    assert result.text == "Translated\n\nTranslated"
    assert result.chunk_providers == ("deepl", "deepl")
    assert attempts == ["dlx", "cloud_ai", "deepl", "deepl"]
    assert cloud_mock.call_count == 1


def test_cloud_auth_does_not_fall_through_to_deepl() -> None:
    cloud = Mock(spec=BlockTranslator)
    cloud.name = "cloud_ai"
    cloud.translate_block.side_effect = CloudAuthError("rejected")
    official = Mock(spec=Translator)
    official.name = "deepl"
    route = HybridRouter(cloud=cloud, official=official).select_route("Source")
    assert route is not None
    outcome = TranslationJob(translator=route).run("Source", Language.EN, Language.UK)
    assert isinstance(outcome.error, CloudAuthError)
    official.translate.assert_not_called()


def test_block_failover_splits_by_utf16_and_preserves_shape_and_dispatch() -> None:
    cloud = Mock(spec=BlockTranslator)
    cloud.name = "cloud_ai"
    cloud.translate_block.side_effect = ProviderUnavailableError("offline")
    official = Mock(spec=Translator)
    official.name = "deepl"
    starts: list[float] = []
    current = 0.0

    def translate(texts: Sequence[str], *_args: object) -> list[TranslationResult]:
        assert sum(map(utf16_len, texts)) <= 5000
        starts.append(current)
        return [TranslationResult(text.upper(), Language.EN, "deepl") for text in texts]

    def wait(seconds: float, cancel: Event) -> bool:
        nonlocal current
        assert not cancel.is_set()
        current += seconds
        return True

    official.translate.side_effect = translate
    source = "\U0001f600" * 2501 + "  \r\nend"
    block = ContextBlock((IndexedSegment(10, source), IndexedSegment(3, "sentence")), ("context",))
    dispatch = RequestDispatch(Event(), 2, 1, wait, clock=RequestStartClock(lambda: current))
    route = BlockFallbackTranslator(cloud, official)
    outcomes = route.translate_block(
        block, Language.EN, Language.UK, TranslationOptions(), dispatch
    )
    assert [outcome.index for outcome in outcomes] == [10, 3]
    assert [outcome.result.text for outcome in outcomes] == [source.upper(), "SENTENCE"]
    assert all(outcome.result.provider == "deepl" for outcome in outcomes)
    assert starts == [2.0, 4.0, 6.0]
    assert cloud.translate_block.call_args.args[0] is block
