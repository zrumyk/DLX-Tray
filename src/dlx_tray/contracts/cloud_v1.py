"""Pydantic wire schemas for the Cloud AI v1 API."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import (
    AnalysisTier,
    Language,
    NoteKind,
    StylisticRegister,
)
from dlx_tray.core.profiles import PROVIDER_LIMITS


def _nonblank(text: str) -> str:
    if not text.strip():
        raise ValueError("text must not be blank")
    return text


type Nonblank = Annotated[str, AfterValidator(_nonblank)]
type Count = Annotated[int, Field(strict=True, ge=0)]


class CloudModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, serialize_by_alias=True)


class ErrorCode(StrEnum):
    INVALID_TOKEN = "invalid_token"
    TOKEN_REVOKED = "token_revoked"
    QUOTA_EXCEEDED = "quota_exceeded"
    DEEP_TIER_FORBIDDEN = "deep_tier_forbidden"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


class ErrorResponse(CloudModel):
    code: ErrorCode
    message: Nonblank


class MeResponse(CloudModel):
    label: Nonblank
    allow_deep_tier: bool
    daily_chars_remaining: Count
    monthly_chars_remaining: Count


class SourceSegment(CloudModel):
    index: Count
    text: Nonblank


class GlossaryTerm(CloudModel):
    source_term: Nonblank
    target_term: Nonblank
    priority: Annotated[int, Field(strict=True)] = 0


class Note(CloudModel):
    kind: NoteKind
    text: Nonblank


class _SourceRequest(CloudModel):
    source_lang: Language = Language.AUTO
    target_lang: Language

    @model_validator(mode="after")
    def _valid_languages(self) -> Self:
        if not self.source_lang.can_be_source or not self.target_lang.can_be_target:
            raise ValueError("invalid source or target language")
        return self


def _validate_indexes(segments: list[SourceSegment] | list[TranslatedSegment]) -> None:
    if len({segment.index for segment in segments}) != len(segments):
        raise ValueError("segment indexes must be unique")


class TranslateRequest(_SourceRequest):
    segments: list[SourceSegment] = Field(min_length=1, max_length=25)
    stylistic_register: StylisticRegister = Field(StylisticRegister.NEUTRAL, alias="register")
    style_context: Annotated[str, Field(max_length=200)] = ""
    context_before: list[Nonblank] = Field(default_factory=list, max_length=2)
    glossary: list[GlossaryTerm] = Field(default_factory=list, max_length=150)
    include_notes: bool = False
    analysis_tier: AnalysisTier = AnalysisTier.STANDARD

    @model_validator(mode="after")
    def _validate_block(self) -> Self:
        _validate_indexes(self.segments)
        units, _, count, context_units = PROVIDER_LIMITS["cloud_ai", self.analysis_tier]
        if len(self.segments) > count or sum(utf16_len(s.text) for s in self.segments) > units:
            raise ValueError("segments exceed the analysis tier budget")
        if sum(map(utf16_len, self.context_before)) > context_units:
            raise ValueError("context_before exceeds the analysis tier budget")
        return self


class TranslatedSegment(CloudModel):
    index: Count
    text: str
    detected_source: Language | None = None
    notes: list[Note] = Field(default_factory=list)
    cached: bool = False


class TranslateResponse(CloudModel):
    segments: list[TranslatedSegment] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def _unique_indexes(self) -> Self:
        _validate_indexes(self.segments)
        return self


class ExtractTermsRequest(_SourceRequest):
    segments: list[Nonblank] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_budget(self) -> Self:
        if sum(map(utf16_len, self.segments)) > 60_000:
            raise ValueError("term extraction exceeds the UTF-16 budget")
        return self


class ExtractedTerm(CloudModel):
    source_term: Nonblank
    target_term: Nonblank
    category: Nonblank
    count: Annotated[int, Field(strict=True, ge=1)]
    note: str | None = None


class ExtractTermsResponse(CloudModel):
    terms: list[ExtractedTerm]
