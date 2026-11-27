"""HTTP client for the local DLX server's ``POST /translate`` (see .dev/docs/DEEPLX.md)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from threading import Condition, Event
from time import monotonic
from types import TracebackType
from typing import Any, Literal, Self

import httpx

from dlx_tray.core.diagnostics import DiagnosticTimeline, ProviderAttempt
from dlx_tray.core.models import (
    AuthError,
    Formality,
    Language,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationCancelledError,
    TranslationError,
    TranslationResult,
)
from dlx_tray.core.retry import DispatchTicket, RequestDispatch, RequestStartClock, event_sleep
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
        diagnostics: DiagnosticTimeline | None = None,
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
        self._diagnostics = diagnostics
        self._owns_client = client is None
        self._dispatch = Condition()
        self._active = 0
        self.start_clock = RequestStartClock(lambda: monotonic())
        self._cooldown_generation = 0
        self._recovering = False
        self._blocked_until = 0.0
        self._cooldown_origin: DispatchTicket | None = None
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
        *,
        dispatch: RequestDispatch | None = None,
        minimum_cooldown: float = 0.0,
    ) -> list[TranslationResult]:
        if not texts:
            return []
        if not target.can_be_target:
            raise ValueError(f"{target.value!r} is not a valid target language")
        if not source.can_be_source:
            raise ValueError(f"{source.value!r} is not a valid source language")
        payload = {
            "text": list(texts),
            "source_lang": source.value,
            "target_lang": target.value,
        }
        log.debug("DLX request: %d texts, %s -> %s", len(texts), source, target)
        if dispatch is None:
            dispatch = RequestDispatch(Event(), 0, 1, event_sleep)
        generation = self._start(dispatch)
        try:
            results = self._request(payload, len(texts))
        except RateLimitedError as error:
            with self._dispatch:
                cooldown = max(minimum_cooldown, error.retry_after or self._next_cooldown)
                self._blocked_until = max(self._blocked_until, monotonic() + cooldown)
                self._cooldown_origin = dispatch.ticket
                self._cooldown_generation += 1
                self._recovering = True
                self._next_cooldown = min(self._next_cooldown * 2, _MAX_COOLDOWN)
            raise RateLimitedError(str(error), retry_after=cooldown) from error
        else:
            with self._dispatch:
                if generation == self._cooldown_generation:
                    self._recovering = False
                    self._next_cooldown = _INITIAL_COOLDOWN
            return results
        finally:
            with self._dispatch:
                self._active -= 1
                self._dispatch.notify_all()

    def _start(self, dispatch: RequestDispatch) -> int:
        while True:
            with self._dispatch:
                if dispatch.cancel.is_set():
                    raise TranslationCancelledError("cancelled before DLX dispatch")
                if not dispatch.may_start():
                    self._dispatch.wait(0.05)
                    continue
                now = monotonic()
                remaining = self._blocked_until - now
                if remaining > 0:
                    if not dispatch.may_observe_cooldown(self._cooldown_origin):
                        self._dispatch.wait(0.05)
                        continue
                    raise RateLimitedError("DLX is in rate-limit cooldown", retry_after=remaining)
                limit = 2 if dispatch.concurrency == 2 and not self._recovering else 1
                if self._active >= limit:
                    self._dispatch.wait(0.05)
                    continue
                pause = (dispatch.clock or self.start_clock).reserve(dispatch)
                if pause <= 0:
                    self._active += 1
                    return self._cooldown_generation
            if not dispatch.sleep(pause, dispatch.cancel):
                raise TranslationCancelledError("cancelled during DLX pacing")

    def _request(self, payload: dict[str, list[str] | str], count: int) -> list[TranslationResult]:
        started = monotonic()
        response = None
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
        finally:
            if self._diagnostics is not None:
                self._record_attempt(started, response)

        assert response is not None
        body = _json_or_none(response)
        if response.status_code != httpx.codes.OK:
            raise _status_error(response, body)
        return _parse_success(body, count)

    def _record_attempt(self, started: float, response: httpx.Response | None) -> None:
        assert self._diagnostics is not None
        elapsed = monotonic() - started
        if response is None:
            self._diagnostics.append(ProviderAttempt(PROVIDER_NAME, elapsed, None))
            return
        cache_header = response.headers.get("X-DLX-Cache", "").strip().lower()
        cache: Literal["hit", "miss"] | None = None
        if cache_header == "hit":
            cache = "hit"
        elif cache_header == "miss":
            cache = "miss"
        self._diagnostics.append(
            ProviderAttempt(
                PROVIDER_NAME,
                elapsed,
                response.status_code,
                parse_retry_after(response.headers.get("Retry-After")),
                cache,
            )
        )


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
