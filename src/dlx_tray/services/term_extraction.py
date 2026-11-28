"""Prepare bounded, deduplicated Pass-1 requests from a translation snapshot."""

from collections.abc import Iterable, Iterator
from threading import Event

from dlx_tray.contracts.cloud_v1 import EXTRACT_TERMS_MAX_UNITS, ExtractTermsRequest
from dlx_tray.core.chunker import HARD_MAX_UNITS, MIN_MAX_UNITS, split_segment, utf16_len
from dlx_tray.core.models import Language, TranslationCancelledError


def _windows(total: int, budget: int) -> tuple[tuple[int, int], ...]:
    if total <= budget or budget < 6:
        return ((0, min(total, budget)),)
    # Spread the unused space between the three passages, leaving room for astral characters
    # in each. The first and last passages reach the ends of the document.
    third = budget // 3
    first = budget - 2 * third
    middle = first + (total - budget) // 2
    return ((0, first), (middle, middle + third), (total - third, total))


def _passages(
    segments: dict[str, int], windows: tuple[tuple[int, int], ...], cancel: Event
) -> Iterator[str]:
    offset = 0
    for text, size in segments.items():
        if cancel.is_set():
            raise TranslationCancelledError("Term extraction was cancelled")
        data: bytes | None = None
        for start, end in windows:
            left = max(0, start - offset)
            right = min(size, end - offset)
            if left >= right:
                continue
            if left == 0 and right == size:
                yield text
            else:
                # A window can split a surrogate pair at either edge.
                if data is None:
                    data = text.encode("utf-16-le", "surrogatepass")
                yield data[2 * left : 2 * right].decode("utf-16-le", "ignore").strip()
        offset += size
        if offset >= windows[-1][1]:
            break


def extraction_request(
    segments: Iterable[str],
    source: Language,
    target: Language,
    *,
    budget: int = EXTRACT_TERMS_MAX_UNITS,
    cancel: Event,
) -> ExtractTermsRequest:
    if not MIN_MAX_UNITS <= budget <= EXTRACT_TERMS_MAX_UNITS:
        raise ValueError("Invalid term extraction budget")
    unique: dict[str, int] = {}
    for segment in segments:
        if cancel.is_set():
            raise TranslationCancelledError("Term extraction was cancelled")
        text = segment.strip()
        if text and text not in unique:
            unique[text] = utf16_len(text)
    windows = _windows(sum(unique.values()), budget)
    sample = dict.fromkeys(
        chunk.text
        for text in _passages(unique, windows, cancel)
        for chunk in split_segment(text, HARD_MAX_UNITS)
        if chunk.translatable
    )
    return ExtractTermsRequest(source_lang=source, target_lang=target, segments=list(sample))
