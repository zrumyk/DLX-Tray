"""Tests for core value objects and errors."""

import dataclasses

import pytest

from dlx_tray.core.models import (
    AuthError,
    Chunk,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)


class TestLanguage:
    def test_values_are_deepl_codes(self) -> None:
        assert Language.AUTO == "auto"
        assert Language.UK == "UK"
        assert Language.EN_US == "EN-US"
        assert Language.EN_GB == "EN-GB"
        assert Language.PT_BR == "PT-BR"
        assert Language.ZH_HANT == "ZH-HANT"

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("auto", Language.AUTO),
            ("AUTO", Language.AUTO),
            (" Auto ", Language.AUTO),
            ("uk", Language.UK),
            ("en-us", Language.EN_US),
            ("EN_GB", Language.EN_GB),
            ("pt-br", Language.PT_BR),
            ("zh-hans", Language.ZH_HANS),
        ],
    )
    def test_from_code(self, code: str, expected: Language) -> None:
        assert Language.from_code(code) is expected

    @pytest.mark.parametrize("code", ["", "XX", "EN-AU", "english"])
    def test_from_code_rejects_unknown(self, code: str) -> None:
        with pytest.raises(ValueError, match="is not a valid Language"):
            Language.from_code(code)

    def test_auto_is_source_only(self) -> None:
        assert Language.AUTO.can_be_source
        assert not Language.AUTO.can_be_target

    @pytest.mark.parametrize(
        "lang",
        [Language.EN_GB, Language.EN_US, Language.PT_BR, Language.PT_PT, Language.ZH_HANT],
    )
    def test_regional_variants_are_target_only(self, lang: Language) -> None:
        assert lang.is_regional
        assert lang.can_be_target
        assert not lang.can_be_source

    @pytest.mark.parametrize("lang", [Language.UK, Language.EN, Language.DE, Language.ZH])
    def test_base_languages_work_both_ways(self, lang: Language) -> None:
        assert not lang.is_regional
        assert lang.can_be_source
        assert lang.can_be_target


def test_formality_values() -> None:
    assert [f.value for f in Formality] == [
        "default",
        "more",
        "less",
        "prefer_more",
        "prefer_less",
    ]


class TestChunk:
    def test_fields_and_equality(self) -> None:
        chunk = Chunk(text="Hello.", separator="\n\n", index=0)
        assert chunk == Chunk("Hello.", "\n\n", 0)
        assert chunk.translatable

    def test_whitespace_only_chunk_is_not_translatable(self) -> None:
        assert not Chunk(text="", separator="  \n", index=0).translatable

    def test_is_frozen(self) -> None:
        chunk = Chunk("a", "", 0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.text = "b"  # type: ignore[misc]


def test_translation_result() -> None:
    result = TranslationResult(text="Привіт", detected_source=Language.EN, provider="dlx")
    assert result.text == "Привіт"
    assert result.detected_source is Language.EN
    assert result.provider == "dlx"


class TestErrors:
    @pytest.mark.parametrize(
        "error_type",
        [RateLimitedError, ProviderUnavailableError, TextTooLongError, AuthError],
    )
    def test_hierarchy(self, error_type: type[TranslationError]) -> None:
        assert issubclass(error_type, TranslationError)
        assert issubclass(TranslationError, Exception)

    def test_rate_limited_carries_retry_after(self) -> None:
        error = RateLimitedError("too many requests", retry_after=12.5)
        assert str(error) == "too many requests"
        assert error.retry_after == 12.5

    def test_rate_limited_retry_after_defaults_to_none(self) -> None:
        assert RateLimitedError("429").retry_after is None
