"""Exercise indexed block packing, bounded concurrency and ordered delivery."""

from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier, Event

import pytest

from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import (
    AnalysisTier,
    ContextBlock,
    Formality,
    IndexedResult,
    Language,
    NoteKind,
    PhilologicalNote,
    TranslationError,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.core.profiles import CLOUD_DEEP_PROFILE, CLOUD_STANDARD_PROFILE, ProviderProfile
from dlx_tray.core.retry import RequestDispatch
from dlx_tray.services.cache import TranslationCache, translation_key


def translate(block: ContextBlock) -> list[IndexedResult]:
    return [
        IndexedResult(
            segment.index, TranslationResult(segment.text.upper(), Language.EN, "cloud_ai")
        )
        for segment in reversed(block.segments)
    ]


class FakeBlocks:
    name = "cloud_ai"

    def __init__(self, send: Callable[[ContextBlock], Sequence[IndexedResult]] = translate) -> None:
        self.send = send
        self.blocks: list[ContextBlock] = []
        self.options: list[TranslationOptions] = []

    def translate_block(
        self,
        block: ContextBlock,
        source: Language,
        target: Language,
        options: TranslationOptions,
        dispatch: RequestDispatch,
    ) -> Sequence[IndexedResult]:
        assert (source, target) == (Language.EN, Language.UK)
        assert 1 <= dispatch.concurrency <= 4
        self.blocks.append(block)
        self.options.append(options)
        return self.send(block)


def test_block_packing_preserves_file_segments_and_uses_cached_context(
    tmp_path: Path,
) -> None:
    cache = TranslationCache(tmp_path / "cache.db")
    source, target = Language.EN, Language.UK
    options = TranslationOptions(include_notes=True)
    key = translation_key("cloud_ai", source, target, Formality.DEFAULT, "cached", options)
    note = PhilologicalNote(NoteKind.IDIOM, "Cached note")
    cache.store(key, TranslationResult("CACHED", source, "cloud_ai", (note,)))
    profile = ProviderProfile("cloud_ai", 9, 1, 25, 7)
    adapter = FakeBlocks()
    job = TranslationJob(translator=adapter, cache=cache, profile=profile)
    segments = [" a ", "bcd", "cached", " e", "fghij", "klmno"]
    output: list[str] = []
    result = job.run(
        "", source, target, file_segments=segments, on_segment=output.append, options=options
    )
    assert output == [segment.upper() for segment in segments]
    assert [[s.text for s in block.segments] for block in adapter.blocks] == [
        ["a", "bcd"],
        ["e", "fghij"],
        ["klmno"],
    ]
    assert [[s.index for s in block.segments] for block in adapter.blocks] == [[0, 1], [3, 4], [5]]
    assert [block.context_before for block in adapter.blocks] == [
        (),
        ("d", "cached"),
        ("e", "fghij"),
    ]
    assert result.segment_notes == ((), (), (note,), (), (), ())
    assert adapter.options == [options] * 3
    with pytest.raises(FrozenInstanceError):
        adapter.blocks[0].context_before = ()  # pyright: ignore[reportAttributeAccessIssue]


def test_four_workers_publish_in_order_despite_reverse_completion() -> None:
    all_running = Barrier(4)
    completed = [Event() for _ in range(4)]
    completion_order: list[int] = []

    def send(block: ContextBlock) -> Sequence[IndexedResult]:
        index = block.segments[0].index // 2
        all_running.wait(timeout=5)
        if index < 3:
            assert completed[index + 1].wait(5)
        completion_order.append(index)
        completed[index].set()
        return translate(block)

    profile = replace(CLOUD_STANDARD_PROFILE, max_concurrency=4, max_segments=2)
    job = TranslationJob(translator=FakeBlocks(send), profile=profile)
    segments = [f"paragraph {index}" for index in range(8)]
    output: list[str] = []
    result = job.run("", Language.EN, Language.UK, file_segments=segments, on_segment=output.append)
    assert completion_order == [3, 2, 1, 0]
    assert output == [segment.upper() for segment in segments]
    assert result.text == "".join(output)


@pytest.mark.parametrize("cancelled", [False, True])
def test_stopped_block_retains_the_completed_prefix(cancelled: bool) -> None:
    cancel = Event()
    count = 0

    def send(block: ContextBlock) -> Sequence[IndexedResult]:
        nonlocal count
        count += 1
        if count == 1 and cancelled:
            cancel.set()
        if count == 2:
            raise TranslationError("test failure")
        return translate(block)

    job = TranslationJob(translator=FakeBlocks(send), profile=ProviderProfile("cloud_ai", 20, 1, 2))
    segments = ["first", "second", "third", "fourth", "fifth"]
    output: list[str] = []
    result = job.run(
        "",
        Language.EN,
        Language.UK,
        cancel=cancel,
        file_segments=segments,
        on_segment=output.append,
    )
    assert output == ["FIRST", "SECOND"]
    assert result.cancelled == cancelled
    assert bool(result.error) != cancelled
    assert count == (1 if cancelled else 2)


def test_wrong_indexes_fail_as_a_whole_block() -> None:
    def send(block: ContextBlock) -> Sequence[IndexedResult]:
        return [translate(block)[0]] * 2

    job = TranslationJob(translator=FakeBlocks(send))
    result = job.run("", Language.EN, Language.UK, file_segments=["one", "two"])
    assert result.text == ""
    assert isinstance(result.error, TranslationError)


def test_deep_budget_splits_and_rejoins_oversized_segments() -> None:
    previous = "\U0001f600" * 1_001
    long = ("a" * CLOUD_DEEP_PROFILE.max_units) + "  end "
    adapter = FakeBlocks()
    job = TranslationJob(translator=adapter)
    output: list[str] = []
    job.run(
        "",
        Language.EN,
        Language.UK,
        file_segments=[previous, long],
        on_segment=output.append,
        options=TranslationOptions(analysis_tier=AnalysisTier.DEEP),
    )
    assert output == [previous, long.upper()]
    assert len(adapter.blocks) == 3
    assert all(
        sum(utf16_len(s.text) for s in block.segments) <= CLOUD_DEEP_PROFILE.max_units
        for block in adapter.blocks
    )
    assert [block.context_before for block in adapter.blocks if block.context_before] == [
        (previous[1:],),
        (previous[1:],),
    ]


@pytest.mark.parametrize(
    "changes",
    [{"max_units": 20_001}, {"max_concurrency": 5}, {"max_segments": 26}, {"context_units": 1_501}],
)
def test_cloud_profile_rejects_limits(changes: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        replace(CLOUD_STANDARD_PROFILE, **changes)
