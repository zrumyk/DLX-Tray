"""Domain value objects and the translation error hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Language(StrEnum):
    """DeepL language codes. ``AUTO`` means "detect the source language"."""

    AUTO = "auto"
    AR = "AR"
    BG = "BG"
    CS = "CS"
    DA = "DA"
    DE = "DE"
    EL = "EL"
    EN = "EN"
    EN_GB = "EN-GB"
    EN_US = "EN-US"
    ES = "ES"
    ET = "ET"
    FI = "FI"
    FR = "FR"
    HU = "HU"
    ID = "ID"
    IT = "IT"
    JA = "JA"
    KO = "KO"
    LT = "LT"
    LV = "LV"
    NB = "NB"
    NL = "NL"
    PL = "PL"
    PT = "PT"
    PT_BR = "PT-BR"
    PT_PT = "PT-PT"
    RO = "RO"
    RU = "RU"
    SK = "SK"
    SL = "SL"
    SV = "SV"
    TR = "TR"
    UK = "UK"
    ZH = "ZH"
    ZH_HANS = "ZH-HANS"
    ZH_HANT = "ZH-HANT"

    @classmethod
    def from_code(cls, code: str) -> Language:
        """Parse a code case-insensitively (``"en-us"``, ``"EN_US"``, ``"Auto"``).

        Raises ``ValueError`` for unknown codes.
        """
        normalized = code.strip().replace("_", "-")
        if normalized.lower() == cls.AUTO.value:
            return cls.AUTO
        return cls(normalized.upper())

    @property
    def is_regional(self) -> bool:
        """A regional variant (``EN-GB``, ``PT-BR``, ``ZH-HANT``...), valid only as a target."""
        return "-" in self.value

    @property
    def can_be_source(self) -> bool:
        return not self.is_regional

    @property
    def can_be_target(self) -> bool:
        return self is not Language.AUTO


class Formality(StrEnum):
    """DeepL formality levels. Only the official API supports them; DLX ignores formality."""

    DEFAULT = "default"
    MORE = "more"
    LESS = "less"
    PREFER_MORE = "prefer_more"
    PREFER_LESS = "prefer_less"


class StylisticRegister(StrEnum):
    NEUTRAL = "neutral"
    LITERARY = "literary"
    ACADEMIC = "academic"
    JOURNALISTIC = "journalistic"
    OFFICIAL = "official"
    LITERAL = "literal"


class AnalysisTier(StrEnum):
    STANDARD = "standard"
    DEEP = "deep"


class NoteKind(StrEnum):
    ALTERNATIVE = "alternative"
    IDIOM = "idiom"
    GRAMMAR = "grammar"


@dataclass(frozen=True, slots=True)
class PhilologicalNote:
    kind: NoteKind
    text: str


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    source_term: str
    target_term: str
    priority: int = 0
    pos: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationOptions:
    analysis_tier: AnalysisTier = AnalysisTier.STANDARD
    register: StylisticRegister = StylisticRegister.NEUTRAL
    style_context: str = ""
    glossary: tuple[GlossaryEntry, ...] = ()
    include_notes: bool = False
    document_glossary: tuple[GlossaryEntry, ...] = ()

    def __post_init__(self) -> None:
        style_context = " ".join(self.style_context.split())
        if len(style_context) > 200:
            raise ValueError("style context must contain at most 200 characters")
        object.__setattr__(self, "style_context", style_context)


DEFAULT_TRANSLATION_OPTIONS = TranslationOptions()


@dataclass(frozen=True, slots=True)
class IndexedSegment:
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class ContextBlock:
    segments: tuple[IndexedSegment, ...]
    context_before: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Chunk:
    """One piece of a split text.

    ``text`` is what gets translated (no leading or trailing whitespace; empty for a
    whitespace-only piece). ``separator`` is the whitespace that followed it in the original
    text and is re-attached verbatim by ``chunker.join``.
    """

    text: str
    separator: str
    index: int

    @property
    def translatable(self) -> bool:
        """Whether the chunk has text worth sending to a provider."""
        return bool(self.text)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """A provider's answer for one text."""

    text: str
    detected_source: Language | None
    provider: str
    notes: tuple[PhilologicalNote, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexedResult:
    index: int
    result: TranslationResult


class TranslationError(Exception):
    """Base class for every translation failure."""


class RateLimitedError(TranslationError):
    """The provider refused the request because of rate limiting (HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LocalCooldownError(RateLimitedError):
    """Tray deferred the request because its provider cooldown is still active."""


class ProviderUnavailableError(TranslationError):
    """The provider is unreachable or failed upstream (connection error, HTTP 503)."""


class TextTooLongError(TranslationError):
    """The request exceeded the provider's length limit (HTTP 413). Indicates a chunker bug."""


class AuthError(TranslationError):
    """The provider reported an authentication failure."""


class CloudAuthError(AuthError):
    """Cloud AI denied access to the service or requested tier."""


class CloudDeepTierForbiddenError(CloudAuthError):
    """The Cloud AI token cannot use deep analysis."""


class CloudQuotaExceededError(TranslationError):
    """The Cloud AI character quota is exhausted."""


class DlxAuthError(AuthError):
    """DLX returned HTTP 401."""


class DlxForbiddenError(TranslationError):
    """DLX returned HTTP 403, which may have originated upstream."""


class TranslationCancelledError(Exception):
    """The user cancelled the operation. Not a ``TranslationError``: cancelling is no failure."""
