"""HTTP client for the local DLX server's ``POST /translate`` (see .dev/docs/DEEPLX.md)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import Any, Self

import httpx

from dlx_tray.core.chunker import HARD_MAX_UNITS, utf16_len
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
from dlx_tray.providers.base import parse_retry_after

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
PROVIDER_NAME = "dlx"
_INITIAL_COOLDOWN = 15.0
_MAX_COOLDOWN = 120.0


class DeeplxTranslator:
    """Translates through DLX. One request per call: all ``texts`` go in one batch.

    DLX has no formality support, so ``formality`` is ignored. Pass an empty token for a
    server without authentication. Pass ``client`` to share or mock the ``httpx.Client``;
    otherwise the translator owns one and ``close()`` closes it.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https"):
            raise ValueError("DLX URL must start with http:// or https://")
        path, separator, query = url.raw_path.partition(b"?")
        self._url = url.copy_with(
            raw_path=path.rstrip(b"/") + b"/translate" + separator + query,
            fragment=None,
        )
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout
        self._owns_client = client is None
        self._request_lock = Lock()
        self._blocked_until = 0.0
        self._next_cooldown = _INITIAL_COOLDOWN
        # trust_env=False: on Windows httpx would pick up the system proxy from the registry and
        # send loopback requests (and the token) through it.
        self._client = client if client is not None else httpx.Client(trust_env=False)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

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
        units = sum(utf16_len(t) for t in texts)
        if units > HARD_MAX_UNITS:
            log.error(
                "DLX batch of %d texts is %d UTF-16 units (limit %d)",
                len(texts),
                units,
                HARD_MAX_UNITS,
            )
            raise TextTooLongError(f"batch is {units} UTF-16 units, DLX allows {HARD_MAX_UNITS}")

        payload = {
            "text": list(texts),
            "source_lang": source.value,
            "target_lang": target.value,
        }
        log.debug("DLX request: %d texts, %d units, %s -> %s", len(texts), units, source, target)
        with self._request_lock:
            remaining = self._blocked_until - monotonic()
            if remaining > 0:
                raise RateLimitedError("DLX is in rate-limit cooldown", retry_after=remaining)
            return self._request(payload, len(texts))

    def _request(self, payload: dict[str, list[str] | str], count: int) -> list[TranslationResult]:
        try:
            response = self._client.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            log.warning("DLX request timed out after %.0f s", self._timeout)
            raise ProviderUnavailableError("DLX did not answer in time") from exc
        except httpx.TransportError as exc:
            log.warning("DLX unreachable: %s", type(exc).__name__)
            raise ProviderUnavailableError("DLX is unreachable") from exc

        body = _json_or_none(response)
        if response.status_code != httpx.codes.OK:
            error = _status_error(response, body)
            if isinstance(error, RateLimitedError):
                delay = error.retry_after or self._next_cooldown
                self._blocked_until = monotonic() + delay
                self._next_cooldown = min(self._next_cooldown * 2, _MAX_COOLDOWN)
                raise RateLimitedError(str(error), retry_after=delay)
            raise error
        results = _parse_success(body, count)
        self._next_cooldown = _INITIAL_COOLDOWN
        return results


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return fallback


def _status_error(response: httpx.Response, body: Any) -> TranslationError:
    status = response.status_code
    message = _message(body, f"HTTP {status}")
    too_large = status == httpx.codes.REQUEST_ENTITY_TOO_LARGE
    # A 413 means a chunk over the limit slipped past the chunker: a bug, hence ERROR.
    log.log(logging.ERROR if too_large else logging.WARNING, "DLX answered HTTP %d", status)
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return AuthError(f"DLX rejected the token: {message}")
    if too_large:
        return TextTooLongError(message)
    if status == httpx.codes.TOO_MANY_REQUESTS:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        return RateLimitedError(message, retry_after=retry_after)
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        return ProviderUnavailableError(message)
    return TranslationError(message)


def _parse_success(body: Any, count: int) -> list[TranslationResult]:
    if not isinstance(body, dict):
        raise ProviderUnavailableError("DLX sent a response that is not a JSON object")
    data: Any = body.get("data")
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(t, str) for t in data):
        raise ProviderUnavailableError("DLX sent a response without translations")
    translations: list[str] = data
    if len(translations) != count:
        raise ProviderUnavailableError(
            f"DLX returned {len(translations)} translations for {count} texts"
        )
    detected = _detected_language(body.get("source_lang"))
    return [TranslationResult(t, detected, PROVIDER_NAME) for t in translations]


def _detected_language(code: Any) -> Language | None:
    if not isinstance(code, str) or not code:
        return None
    try:
        language = Language.from_code(code)
    except ValueError:
        log.debug("DLX reported an unknown source language")
        return None
    return None if language is Language.AUTO else language
