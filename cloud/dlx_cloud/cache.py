"""Anonymous segment cache shared by authorized callers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING

from google.api_core.exceptions import GoogleAPIError
from pydantic import ValidationError

from dlx_tray.contracts.cloud_v1 import TranslatedSegment, TranslateRequest
from dlx_tray.contracts.glossary_match import matched_glossary
from dlx_tray.core.models import AnalysisTier

from .auth import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.cloud.firestore import Client

    from dlx_tray.contracts.cloud_v1 import SourceSegment

PROMPT_SCHEMA_VERSION = "v1"
LOGGER = logging.getLogger(__name__)


def style_context(request: TranslateRequest) -> str:
    return request.style_context.strip()


def segment_key(request: TranslateRequest, segment: SourceSegment, model: str) -> str:
    hasher = sha256()
    for value in (
        PROMPT_SCHEMA_VERSION,
        model,
        request.analysis_tier,
        request.source_lang,
        request.target_lang,
        request.stylistic_register,
        style_context(request),
        json.dumps(matched_glossary(segment.text, request.glossary), ensure_ascii=False),
        str(request.include_notes),
        segment.text,
    ):
        data = value.encode("utf-8")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


class SegmentCache:
    def __init__(self, db: Client, ttl_days: int, *, now: Callable[[], datetime] = utc_now) -> None:
        self._collection = db.collection("translation_cache")
        self._lifetime = timedelta(days=ttl_days)
        self._now = now

    def get(self, key: str, index: int) -> TranslatedSegment | None:
        try:
            data = self._collection.document(key).get().to_dict()
        except GoogleAPIError:
            LOGGER.warning("Segment cache read failed")
            return None
        if data is None:
            return None
        expires = data.get("expires_at")
        if not isinstance(expires, datetime) or expires.tzinfo is None or expires <= self._now():
            return None
        try:
            return TranslatedSegment.model_validate(
                {
                    "index": index,
                    "text": data.get("translation"),
                    "detected_source": data.get("detected_source"),
                    "notes": data.get("notes") or [],
                    "cached": True,
                }
            )
        except ValidationError:
            LOGGER.warning("Invalid segment cache entry")
            return None

    def put(self, key: str, result: TranslatedSegment, tier: AnalysisTier, source: str) -> None:
        # Retain the API result for the caller even when it echoes a complete source segment.
        source = source.strip()
        if source in result.text or any(source in note.text for note in result.notes):
            return
        now = self._now()
        try:
            self._collection.document(key).set(
                {
                    "translation": result.text,
                    "detected_source": (
                        result.detected_source.value if result.detected_source is not None else None
                    ),
                    "notes": [note.model_dump(mode="json") for note in result.notes],
                    "model_tier": tier.value,
                    "created_at": now,
                    "expires_at": now + self._lifetime,
                }
            )
        except GoogleAPIError:
            LOGGER.warning("Segment cache write failed")
