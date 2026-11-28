"""Cloud AI HTTP endpoints."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.api_core.exceptions import GoogleAPIError
from starlette.middleware.base import RequestResponseEndpoint

from dlx_tray.contracts.cloud_v1 import (
    ErrorCode,
    ErrorResponse,
    ExtractTermsRequest,
    ExtractTermsResponse,
    MeResponse,
    SourceSegment,
    TranslatedSegment,
    TranslateRequest,
    TranslateResponse,
)
from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import AnalysisTier

from .auth import TokenStore, invalid_token
from .cache import SegmentCache, segment_key
from .config import CloudSettings
from .errors import CloudError
from .gemini import Gemini, Interactions, create_client, unavailable

LOGGER = logging.getLogger(__name__)
BEARER = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Services:
    tokens: TokenStore
    cache: SegmentCache
    gemini: Gemini


def _services(request: Request) -> Services:
    services = cast(Services | None, getattr(request.app.state, "services", None))
    if services is None:
        raise unavailable()
    return services


type ServiceDep = Annotated[Services, Depends(_services)]


def _authenticate(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
    services: ServiceDep,
) -> str:
    if creds is None:
        raise invalid_token()
    return services.tokens.authenticate(creds.scheme, creds.credentials)


type TokenDep = Annotated[str, Depends(_authenticate)]


def _error_response(error: CloudError) -> JSONResponse:
    body = ErrorResponse(code=error.code, message=error.message)
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
    return JSONResponse(body.model_dump(mode="json"), status_code=error.status, headers=headers)


def _quota_headers(response: Response, remaining: MeResponse) -> None:
    response.headers["X-Quota-Daily-Remaining"] = str(remaining.daily_chars_remaining)
    response.headers["X-Quota-Monthly-Remaining"] = str(remaining.monthly_chars_remaining)


def create_app(settings: CloudSettings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or CloudSettings()

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
        if services is not None:
            instance.state.services = services
            yield
            return
        pepper = settings.token_pepper.get_secret_value()
        api_key = settings.gemini_api_key.get_secret_value()
        if not pepper or not api_key:
            LOGGER.warning("Cloud AI credentials are missing")
            yield
            return
        db = settings.firestore()
        try:
            with create_client(api_key) as client:
                instance.state.services = Services(
                    TokenStore(db, pepper),
                    SegmentCache(db, settings.cache_ttl_days),
                    Gemini(cast(Interactions, client.interactions)),
                )
                yield
        finally:
            instance.state.services = None
            db.close()

    instance = FastAPI(title="DLX Tray Cloud", lifespan=lifespan)

    @instance.exception_handler(CloudError)
    async def cloud_error(_request: Request, error: CloudError) -> JSONResponse:
        return _error_response(error)

    @instance.exception_handler(GoogleAPIError)
    async def firestore_error(_request: Request, _error: GoogleAPIError) -> JSONResponse:
        LOGGER.warning("Firestore request failed")
        return _error_response(unavailable())

    @instance.exception_handler(RequestValidationError)
    async def invalid_body(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request body."}, status_code=422)

    @instance.middleware("http")
    async def format_body_limit(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if response.status_code == 413:
            return _error_response(
                CloudError(413, ErrorCode.PAYLOAD_TOO_LARGE, "Request body is too large.")
            )
        return response

    @instance.get("/healthz")
    def health() -> dict[str, Literal["ok"]]:
        return {"status": "ok"}

    @instance.get("/v1/me")
    def me(token: TokenDep, services: ServiceDep) -> MeResponse:
        return services.tokens.remaining(token)

    @instance.post("/v1/translate", max_body_size=settings.max_body_size)
    def translate(
        request: TranslateRequest, response: Response, token: TokenDep, services: ServiceDep
    ) -> TranslateResponse:
        tier = request.analysis_tier
        services.tokens.check_tier(token, tier)
        model = settings.model_for(tier)
        results: dict[int, TranslatedSegment] = {}
        misses: list[SourceSegment] = []
        missing_keys: dict[int, tuple[str, str]] = {}
        for segment in request.segments:
            key = segment_key(request, segment, model)
            hit = services.cache.get(key, segment.index)
            if hit is None:
                missing_keys[segment.index] = key, segment.text
                misses.append(segment)
            else:
                results[segment.index] = hit

        if misses:
            units = sum(utf16_len(segment.text) for segment in misses)
            translated, remaining = services.gemini.translate(
                request, misses, model, lambda: services.tokens.reserve(token, tier, units)
            )
            for result in translated:
                key, source = missing_keys[result.index]
                services.cache.put(key, result, tier, source)
                results[result.index] = result
        else:
            remaining = services.tokens.remaining(token, tier)

        response.headers["X-Cloud-Cache"] = (
            "hit" if not misses else "miss" if len(misses) == len(request.segments) else "mixed"
        )
        _quota_headers(response, remaining)
        return TranslateResponse(segments=[results[segment.index] for segment in request.segments])

    @instance.post("/v1/extract-terms", max_body_size=settings.max_body_size)
    def extract_terms(
        request: ExtractTermsRequest, response: Response, token: TokenDep, services: ServiceDep
    ) -> ExtractTermsResponse:
        tier = AnalysisTier.DEEP
        units = sum(map(utf16_len, request.segments))
        result, remaining = services.gemini.extract_terms(
            request, settings.model_for(tier), lambda: services.tokens.reserve(token, tier, units)
        )
        _quota_headers(response, remaining)
        return result

    return instance


app = create_app()
