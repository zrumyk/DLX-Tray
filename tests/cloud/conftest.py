from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore import Client

from dlx_cloud.app import Services, create_app
from dlx_cloud.auth import TokenRecord, TokenStore
from dlx_cloud.cache import SegmentCache
from dlx_cloud.config import CloudSettings
from dlx_cloud.gemini import Gemini

from .fake_firestore import FirestoreFake

PEPPER = "fixture-pepper-only".ljust(32, "!")
DEFAULT_REQUEST = {"segments": [{"index": 3, "text": "Source"}], "target_lang": "UK"}


class Clock:
    now = datetime(2026, 11, 28, tzinfo=UTC)
    elapsed = 0.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed


@dataclass
class Output:
    output_text: str | None


class FakeInteractions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.queue: list[str | Exception | None] = []

    def create(self, **body: object) -> Output:
        self.calls.append(body)
        result = self.queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return Output(result)


@dataclass
class Harness:
    db: FirestoreFake
    sdk: FakeInteractions
    clock: Clock
    services: Services
    settings: CloudSettings

    def issue(self, **fields: object) -> tuple[str, str]:
        return self.services.tokens.create(
            TokenRecord.model_validate({"created_at": self.clock.now, "label": "User", **fields})
        )

    def client(self) -> TestClient:
        return TestClient(create_app(self.settings, self.services))


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def harness() -> Harness:
    db = FirestoreFake()
    client = cast(Client, db)
    sdk = FakeInteractions()
    clock = Clock()
    settings = CloudSettings()
    services = Services(
        TokenStore(client, PEPPER, now=clock.utc_now, monotonic=clock.monotonic),
        SegmentCache(client, 60, now=clock.utc_now),
        Gemini(sdk, settings.max_body_size),
    )
    return Harness(db, sdk, clock, services, settings)


@pytest.fixture
def http(harness: Harness) -> Iterator[TestClient]:
    with harness.client() as client:
        yield client
