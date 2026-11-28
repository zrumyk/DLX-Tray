"""Desktop HTTP client for the Cloud AI v1 proxy."""

from __future__ import annotations

from threading import Event
from time import monotonic

import httpx
from pydantic import BaseModel, ValidationError

from dlx_tray.contracts.cloud_v1 import (
    ErrorCode,
    ErrorResponse,
    ExtractTermsRequest,
    ExtractTermsResponse,
    GlossaryTerm,
    MeResponse,
    SourceSegment,
    TranslateRequest,
    TranslateResponse,
)
from dlx_tray.core.diagnostics import DiagnosticTimeline, ProviderAttempt
from dlx_tray.core.models import (
    AnalysisTier,
    CloudAuthError,
    CloudDeepTierForbiddenError,
    CloudQuotaExceededError,
    ContextBlock,
    IndexedResult,
    Language,
    PhilologicalNote,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationCancelledError,
    TranslationError,
    TranslationOptions,
    TranslationResult,
)
from dlx_tray.core.retry import RequestDispatch
from dlx_tray.providers.base import parse_retry_after

PROVIDER_NAME = "cloud_ai"
# The proxy can make two Gemini attempts, each with a 120-second deadline.
DEFAULT_TIMEOUT = 250.0


class CloudAiTranslator:
    name = PROVIDER_NAME

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        diagnostics: DiagnosticTimeline | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            trust_env=True,
        )
        self._diagnostics = diagnostics

    def close(self) -> None:
        self._client.close()

    def check_token(self, *, cancel: Event | None = None) -> MeResponse:
        return self._request("GET", "v1/me", MeResponse, cancel=cancel)

    def extract_terms(
        self, request: ExtractTermsRequest, *, cancel: Event | None = None
    ) -> ExtractTermsResponse:
        return self._request("POST", "v1/extract-terms", ExtractTermsResponse, request, cancel)

    def translate_block(
        self,
        block: ContextBlock,
        source: Language,
        target: Language,
        options: TranslationOptions,
        dispatch: RequestDispatch,
    ) -> list[IndexedResult]:
        if not block.segments:
            return []
        try:
            request = TranslateRequest(
                source_lang=source,
                target_lang=target,
                segments=[SourceSegment(index=s.index, text=s.text) for s in block.segments],
                context_before=list(block.context_before),
                register=options.register,
                style_context=options.style_context,
                glossary=[
                    GlossaryTerm(
                        source_term=t.source_term, target_term=t.target_term, priority=t.priority
                    )
                    for t in options.glossary
                ],
                include_notes=options.include_notes,
                analysis_tier=options.analysis_tier,
            )
        except ValidationError:
            raise TranslationError("Invalid Cloud AI translation block") from None
        response = self._request(
            "POST", "v1/translate", TranslateResponse, request, dispatch.cancel
        )
        indexed = {segment.index: segment for segment in response.segments}
        if indexed.keys() != {segment.index for segment in block.segments}:
            raise ProviderUnavailableError("Cloud AI returned mismatched segment indexes")
        results = []
        for segment in block.segments:
            item = indexed[segment.index]
            result = TranslationResult(
                item.text,
                item.detected_source,
                self.name,
                tuple(PhilologicalNote(note.kind, note.text) for note in item.notes),
            )
            results.append(IndexedResult(item.index, result))
        return results

    def _request[T: BaseModel](
        self,
        method: str,
        path: str,
        response_type: type[T],
        body: BaseModel | None = None,
        cancel: Event | None = None,
    ) -> T:
        if cancel is not None and cancel.is_set():
            raise TranslationCancelledError("Cancelled before Cloud AI request")
        if (
            not self._token
            or not self._token.isascii()
            or not self._token.isprintable()
            or " " in self._token
        ):
            raise CloudAuthError("Cloud AI token must contain only printable ASCII without spaces")
        started = monotonic()
        response = None
        try:
            response = self._client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {self._token}"},
                json=body.model_dump(mode="json") if body is not None else None,
            )
        except (httpx.HTTPError, UnicodeError):
            if cancel is not None and cancel.is_set():
                raise TranslationCancelledError("Cancelled during Cloud AI request") from None
            raise ProviderUnavailableError("Could not reach Cloud AI") from None
        finally:
            if self._diagnostics is not None:
                headers = response.headers if response is not None else {}
                cache = headers.get("X-Cloud-Cache")
                tier = None
                if isinstance(body, TranslateRequest):
                    tier = body.analysis_tier
                elif isinstance(body, ExtractTermsRequest):
                    tier = AnalysisTier.DEEP
                self._diagnostics.append(
                    ProviderAttempt(
                        PROVIDER_NAME,
                        monotonic() - started,
                        response.status_code if response is not None else None,
                        parse_retry_after(headers.get("Retry-After")),
                        cache if cache in ("hit", "miss", "mixed") else None,
                        tier,
                    )
                )
        if cancel is not None and cancel.is_set():
            raise TranslationCancelledError("Cancelled during Cloud AI request")
        assert response is not None
        if not response.is_success:
            raise _response_error(response)
        try:
            return response_type.model_validate_json(response.content)
        except ValidationError:
            raise ProviderUnavailableError("Cloud AI returned an invalid response") from None


def _response_error(response: httpx.Response) -> TranslationError:
    try:
        code = ErrorResponse.model_validate_json(response.content).code
    except ValidationError:
        code = None
    match response.status_code:
        case 401:
            message = (
                "Cloud AI token was revoked"
                if code is ErrorCode.TOKEN_REVOKED
                else "Cloud AI rejected the token"
            )
            return CloudAuthError(message)
        case 403 if code is ErrorCode.DEEP_TIER_FORBIDDEN:
            return CloudDeepTierForbiddenError("Cloud AI token cannot use deep analysis")
        case 403:
            return CloudAuthError("Cloud AI denied access")
        case 413:
            return TextTooLongError("Cloud AI request is too large")
        case 429 if code is ErrorCode.QUOTA_EXCEEDED:
            return CloudQuotaExceededError("Cloud AI character quota is exhausted")
        case 429:
            return RateLimitedError(
                "Cloud AI rate limit reached",
                parse_retry_after(response.headers.get("Retry-After")),
            )
        case status if status >= 500:
            return ProviderUnavailableError("Cloud AI is unavailable")
        case _:
            return TranslationError(f"Cloud AI request failed (HTTP {response.status_code})")
