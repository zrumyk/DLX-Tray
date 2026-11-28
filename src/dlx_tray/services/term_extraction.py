"""Prepare bounded, deduplicated Pass-1 requests from a translation snapshot."""

from collections.abc import Iterable
from threading import Event

from dlx_tray.contracts.cloud_v1 import EXTRACT_TERMS_MAX_UNITS, ExtractTermsRequest
from dlx_tray.core.chunker import HARD_MAX_UNITS, MIN_MAX_UNITS, split_segment, utf16_len
from dlx_tray.core.models import Language, TranslationCancelledError


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
    seen_segments: set[str] = set()
    sample: dict[str, None] = {}
    remaining = budget
    for segment in segments:
        if cancel.is_set():
            raise TranslationCancelledError("Term extraction was cancelled")
        text = segment.strip()
        if not text or text in seen_segments:
            continue
        seen_segments.add(text)
        for chunk in split_segment(text, min(budget, HARD_MAX_UNITS)):
            if cancel.is_set():
                raise TranslationCancelledError("Term extraction was cancelled")
            if not chunk.translatable or chunk.text in sample:
                continue
            size = utf16_len(chunk.text)
            if size > remaining:
                if remaining >= MIN_MAX_UNITS:
                    for prefix in split_segment(chunk.text, remaining):
                        if prefix.translatable:
                            sample.setdefault(prefix.text, None)
                            break
                remaining = 0
                break
            sample[chunk.text] = None
            remaining -= size
        if remaining == 0:
            break
    return ExtractTermsRequest(source_lang=source, target_lang=target, segments=list(sample))
