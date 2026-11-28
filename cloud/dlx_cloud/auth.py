"""Hashed bearer token authentication and Firestore quota accounting."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime
from hmac import digest
from threading import Lock
from typing import TYPE_CHECKING, cast

from google.api_core.exceptions import Aborted
from google.cloud import firestore
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from dlx_tray.contracts.cloud_v1 import Count, ErrorCode, MeResponse, Nonblank
from dlx_tray.core.models import AnalysisTier

from .errors import CloudError

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN_PATTERN = re.compile(r"dlxt_[A-Za-z0-9_-]{43}")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}")
AUTH_CACHE_SECONDS = 60
AUTH_CACHE_CAPACITY = 1024


def utc_now() -> datetime:
    return datetime.now(UTC)


def token_hash(pepper: str, token: str) -> str:
    return digest(pepper.encode(), token.encode(), "sha256").hex()


class TokenRecord(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    label: Nonblank
    role: str = "translator"
    active: bool = True
    allow_deep_tier: bool = False
    daily_char_limit: Count = 250_000
    monthly_char_limit: Count = 3_000_000
    daily_chars_used: Count = 0
    daily_period: str = ""
    monthly_chars_used: Count = 0
    monthly_period: str = ""
    created_at: datetime
    expires_at: AwareDatetime | None = None

    def authorize(self, now: datetime, tier: AnalysisTier) -> None:
        if not self.active:
            raise CloudError(401, ErrorCode.TOKEN_REVOKED, "Token was revoked.")
        if self.expires_at is not None and self.expires_at <= now:
            raise invalid_token()
        if tier is AnalysisTier.DEEP and not self.allow_deep_tier:
            raise CloudError(403, ErrorCode.DEEP_TIER_FORBIDDEN, "Deep analysis is unavailable.")

    def usage(self, now: datetime) -> tuple[int, int]:
        return (
            self.daily_chars_used if self.daily_period == now.date().isoformat() else 0,
            self.monthly_chars_used if self.monthly_period == now.strftime("%Y-%m") else 0,
        )

    def remaining(self, now: datetime) -> MeResponse:
        daily, monthly = self.usage(now)
        return MeResponse(
            label=self.label,
            allow_deep_tier=self.allow_deep_tier,
            daily_chars_remaining=max(0, self.daily_char_limit - daily),
            monthly_chars_remaining=max(0, self.monthly_char_limit - monthly),
        )


def invalid_token() -> CloudError:
    return CloudError(401, ErrorCode.INVALID_TOKEN, "Invalid or expired bearer token.")


def run_transaction[T](db: firestore.Client, operation: Callable[[firestore.Transaction], T]) -> T:
    # The SDK's decorator keeps retry state on its callable. Create one per invocation.
    try:
        return cast(T, firestore.transactional(operation)(db.transaction()))
    except ValueError as error:
        if isinstance(error.__cause__, Aborted):
            raise error.__cause__ from None
        raise


class TokenStore:
    def __init__(
        self,
        db: firestore.Client,
        pepper: str,
        *,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(pepper) < 32:
            raise ValueError("DLX_CLOUD_TOKEN_PEPPER must contain at least 32 characters.")
        self._db = db
        self._tokens = db.collection("api_tokens")
        self._pepper = pepper
        self._now = now
        self._monotonic = monotonic
        self._cache: OrderedDict[str, tuple[float, TokenRecord]] = OrderedDict()
        self._lock = Lock()

    def _read(self, key: str, transaction: firestore.Transaction | None = None) -> TokenRecord:
        fields = self._tokens.document(key).get(transaction=transaction).to_dict()
        if fields is None:
            raise invalid_token()
        try:
            return TokenRecord.model_validate(fields)
        except ValidationError:
            raise invalid_token() from None

    def authenticate(self, scheme: str, token: str) -> str:
        if scheme.lower() != "bearer" or TOKEN_PATTERN.fullmatch(token) is None:
            raise invalid_token()
        key = token_hash(self._pepper, token)
        started = self._monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry[0] > started:
                self._cache.move_to_end(key)
                entry[1].authorize(self._now(), AnalysisTier.STANDARD)
                return key

        record = self._read(key)
        record.authorize(self._now(), AnalysisTier.STANDARD)
        with self._lock:
            self._cache[key] = (started + AUTH_CACHE_SECONDS, record)
            self._cache.move_to_end(key)
            if len(self._cache) > AUTH_CACHE_CAPACITY:
                self._cache.popitem(last=False)
        return key

    def check_tier(self, key: str, tier: AnalysisTier) -> None:
        if tier is AnalysisTier.DEEP:
            # Tier authorization applies even if every requested segment is cached.
            self._read(key).authorize(self._now(), tier)

    def remaining(self, key: str, tier: AnalysisTier = AnalysisTier.STANDARD) -> MeResponse:
        record = self._read(key)
        now = self._now()
        record.authorize(now, tier)
        return record.remaining(now)

    def reserve(self, key: str, tier: AnalysisTier, units: int) -> MeResponse:
        def increment(transaction: firestore.Transaction) -> MeResponse:
            record = self._read(key, transaction)
            now = self._now()
            record.authorize(now, tier)
            remaining = record.remaining(now)
            if units > min(remaining.daily_chars_remaining, remaining.monthly_chars_remaining):
                raise CloudError(429, ErrorCode.QUOTA_EXCEEDED, "Translation quota exceeded.")
            daily, monthly = record.usage(now)
            transaction.update(
                self._tokens.document(key),
                {
                    "daily_chars_used": daily + units,
                    "daily_period": now.date().isoformat(),
                    "monthly_chars_used": monthly + units,
                    "monthly_period": now.strftime("%Y-%m"),
                },
            )
            return remaining.model_copy(
                update={
                    "daily_chars_remaining": remaining.daily_chars_remaining - units,
                    "monthly_chars_remaining": remaining.monthly_chars_remaining - units,
                },
            )

        return run_transaction(self._db, increment)

    def _new_token(self) -> tuple[str, str]:
        token = f"dlxt_{secrets.token_urlsafe(32)}"
        return token, token_hash(self._pepper, token)

    def create(self, record: TokenRecord) -> tuple[str, str]:
        token, key = self._new_token()
        self._tokens.document(key).create(record.model_dump())
        return token, key

    def list(self) -> list[tuple[str, TokenRecord]]:
        return [
            (snapshot.id, TokenRecord.model_validate(snapshot.to_dict()))
            for snapshot in self._tokens.stream()
        ]

    def inspect(self, key: str) -> tuple[TokenRecord, tuple[int, int]]:
        record = self._read(key)
        return record, record.usage(self._now())

    def revoke(self, key: str) -> None:
        self._tokens.document(key).update({"active": False})
        with self._lock:
            self._cache.pop(key, None)

    def rotate(self, key: str) -> tuple[str, str]:
        token, new_key = self._new_token()

        def replace(transaction: firestore.Transaction) -> None:
            record = self._read(key, transaction)
            now = self._now()
            record.authorize(now, AnalysisTier.STANDARD)
            replacement = record.model_copy(update={"created_at": now})
            transaction.create(self._tokens.document(new_key), replacement.model_dump())
            transaction.update(self._tokens.document(key), {"active": False})

        run_transaction(self._db, replace)
        with self._lock:
            self._cache.pop(key, None)
        return token, new_key
