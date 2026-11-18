"""Translation through the official DeepL API."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from http import HTTPStatus

import deepl

from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
    TranslationResult,
)

log = logging.getLogger(__name__)

PROVIDER_NAME = "deepl"
DEFAULT_TIMEOUT = 30.0


class DeepLOfficialTranslator:
    """Use the SDK's key-based Free/Pro routing and let the job handle retries."""

    def __init__(self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = deepl.DeepLClient(api_key, max_retries=0, timeout_s=timeout)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def close(self) -> None:
        self._client.close()

    def translate(
        self,
        texts: Sequence[str],
        source: Language,
        target: Language,
        formality: Formality = Formality.DEFAULT,
    ) -> list[TranslationResult]:
        if not texts:
            return []
        if not target.can_be_target:
            raise ValueError(f"{target.value!r} is not a valid target language")
        if not source.can_be_source:
            raise ValueError(f"{source.value!r} is not a valid source language")

        try:
            results = self._client.translate_text(
                list(texts),
                source_lang=None if source is Language.AUTO else source.value,
                target_lang=target.value,
                formality=formality.value,
            )
        except deepl.DeepLException as exc:
            # SDK error details can contain the response body.
            log.warning(
                "DeepL translation failed: %s (HTTP %s)", type(exc).__name__, exc.http_status_code
            )
            raise _translation_error(exc) from None
        except (AttributeError, TypeError):
            # The SDK may encounter unexpected JSON shapes while parsing a successful response.
            log.warning("DeepL returned a malformed translation response")
            raise ProviderUnavailableError(
                "DeepL returned a malformed translation response"
            ) from None

        if (
            not isinstance(results, list)
            or len(results) != len(texts)
            or any(
                not isinstance(result.text, str) or (original.strip() and not result.text.strip())
                for original, result in zip(texts, results, strict=True)
            )
        ):
            log.warning("DeepL returned invalid results for %d texts", len(texts))
            raise ProviderUnavailableError("DeepL returned invalid translation results")
        return [
            TranslationResult(
                result.text, _detected_language(result.detected_source_lang), self.name
            )
            for result in results
        ]


def _translation_error(exc: deepl.DeepLException) -> TranslationError:
    status = exc.http_status_code
    if isinstance(exc, deepl.AuthorizationException):
        return AuthError("DeepL rejected the API key")
    if isinstance(exc, deepl.TooManyRequestsException):
        return RateLimitedError("DeepL rate limit reached")
    if isinstance(exc, deepl.QuotaExceededException):
        return TranslationError("DeepL translation quota exceeded")
    if isinstance(exc, deepl.ConnectionException) and not exc.should_retry:
        return TranslationError("Could not connect to DeepL. Check the network and proxy settings")
    if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
        return TextTooLongError("The translation request exceeds the DeepL size limit")
    if status is None or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return ProviderUnavailableError("DeepL could not provide a translation")
    return TranslationError(f"DeepL rejected the translation request (HTTP {status})")


def _detected_language(code: object) -> Language | None:
    if not isinstance(code, str):
        return None
    try:
        language = Language.from_code(code)
    except ValueError:
        log.debug("DeepL reported an unknown source language")
        return None
    return None if language is Language.AUTO else language
