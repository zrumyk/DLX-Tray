import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore import Client

from dlx_cloud.auth import TokenStore
from dlx_cloud.cli import main
from dlx_cloud.config import CloudSettings
from dlx_cloud.errors import CloudError
from dlx_tray.core.models import AnalysisTier

from .conftest import DEFAULT_REQUEST, PEPPER, Harness, headers


def test_health_and_bearer_errors(harness: Harness, http: TestClient) -> None:
    assert http.get("/healthz").json() == {"status": "ok"}
    token, key = harness.issue()
    expected = hmac.digest(PEPPER.encode(), token.encode(), hashlib.sha256).hex()
    assert key == expected
    assert http.get("/v1/me", headers=headers(token)).json() == {
        "label": "User",
        "allow_deep_tier": False,
        "daily_chars_remaining": 250_000,
        "monthly_chars_remaining": 3_000_000,
    }
    revoked, _ = harness.issue(active=False)
    expired, _ = harness.issue(expires_at=harness.clock.now)
    malformed, malformed_key = harness.issue()
    harness.db.data["api_tokens"][malformed_key]["expires_at"] = "2026-11-28T00:00:00"
    unauthorized = [
        ({}, "invalid_token"),
        ({"Authorization": "Basic invalid"}, "invalid_token"),
        (headers("bad-secret"), "invalid_token"),
        (headers(f"dlxt_{'a' * 43}"), "invalid_token"),
        (headers(revoked), "token_revoked"),
        (headers(expired), "invalid_token"),
        (headers(malformed), "invalid_token"),
    ]
    for auth, code in unauthorized:
        response = http.get("/v1/me", headers=auth)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["code"] == code
    assert all(token not in repr(fields) for fields in harness.db.data.values())


def test_auth_cache_deadline_and_token_expiry(harness: Harness) -> None:
    store = harness.services.tokens
    token, key = harness.issue(expires_at=harness.clock.now + timedelta(seconds=1))
    assert store.authenticate("bearer", token) == key
    assert store.authenticate("Bearer", token) == key
    assert harness.db.reads["api_tokens", key] == 1
    harness.clock.now += timedelta(seconds=1)
    with pytest.raises(CloudError, match="expired"):
        store.authenticate("Bearer", token)

    token, key = harness.issue()
    assert store.authenticate("Bearer", token) == key
    harness.db.data["api_tokens"][key]["active"] = False
    harness.clock.elapsed = 59
    assert store.authenticate("Bearer", token) == key
    harness.clock.elapsed = 60
    with pytest.raises(CloudError, match="revoked"):
        store.authenticate("Bearer", token)
    assert harness.db.reads["api_tokens", key] == 2


def test_quota_rollovers_and_contention(harness: Harness) -> None:
    store = harness.services.tokens
    _, key = harness.issue(daily_char_limit=3, monthly_char_limit=5)
    tier = AnalysisTier.STANDARD

    def reserve() -> bool:
        try:
            store.reserve(key, tier, 1)
        except CloudError as error:
            assert error.code == "quota_exceeded"
            return False
        return True

    harness.db.aborts = 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: reserve(), range(10))) == 3
    assert store.inspect(key)[1] == (3, 3)
    harness.clock.now += timedelta(days=1)
    assert store.reserve(key, tier, 2).monthly_chars_remaining == 0
    with pytest.raises(CloudError, match="quota"):
        store.reserve(key, tier, 1)
    assert store.inspect(key)[1] == (2, 5)
    harness.clock.now = datetime(2026, 12, 1, tzinfo=UTC)
    assert store.reserve(key, tier, 1).monthly_chars_remaining == 4
    assert store.inspect(key)[1] == (1, 1)


def test_revoked_token_rechecked_and_transaction_exhaustion(
    harness: Harness, http: TestClient
) -> None:
    token, key = harness.issue()
    harness.db.aborts = 5
    response = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"
    assert not harness.sdk.calls
    assert harness.services.tokens.inspect(key)[1] == (0, 0)

    harness.db.data["api_tokens"][key]["active"] = False
    response = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert response.json()["code"] == "token_revoked"
    assert not harness.sdk.calls


def test_cli_token_lifecycle(monkeypatch: pytest.MonkeyPatch, capsys, harness: Harness) -> None:
    monkeypatch.setenv("DLX_CLOUD_TOKEN_PEPPER", PEPPER)
    monkeypatch.setattr(CloudSettings, "firestore", lambda _: harness.db)

    harness.clock.now = datetime.now(UTC)
    assert main(["create", "Reader", "--allow-deep-tier", "--daily-limit", "12"]) == 0
    issued = json.loads(capsys.readouterr().out)
    token, key = issued["token"], issued["token_hash"]
    assert harness.services.tokens.authenticate("Bearer", token) == key
    harness.services.tokens.reserve(key, AnalysisTier.DEEP, 3)

    assert main(["list"]) == 0
    listed = capsys.readouterr().out
    assert json.loads(listed)[0]["token_hash"] == key
    assert token not in listed

    assert main(["rotate", key]) == 0
    replacement = json.loads(capsys.readouterr().out)
    new = replacement["token_hash"]
    assert replacement["token"] != token
    assert harness.services.tokens.authenticate("Bearer", replacement["token"]) == new
    assert main(["usage", new]) == 0
    usage = capsys.readouterr().out
    assert json.loads(usage)["daily_chars_used"] == 3
    assert json.loads(usage)["daily_char_limit"] == 12
    assert replacement["token"] not in usage
    assert main(["revoke", new]) == 0
    assert main(["rotate", new]) == 1
    assert "failed" in capsys.readouterr().err
    assert harness.db.closes == 6
    assert not harness.db.data["api_tokens"][key]["active"]
    assert not harness.db.data["api_tokens"][new]["active"]
    documents = repr(harness.db.data)
    assert token not in documents and replacement["token"] not in documents


def test_cli_refuses_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, capsys
) -> None:
    monkeypatch.delenv("DLX_CLOUD_TOKEN_PEPPER", raising=False)
    assert main(["list"]) == 1
    monkeypatch.setenv("DLX_CLOUD_TOKEN_PEPPER", PEPPER)
    monkeypatch.setattr(CloudSettings, "firestore", lambda _: harness.db)

    with pytest.raises(SystemExit) as error:
        main(["usage", "dlxt_secret_argument"])
    assert error.value.code == 2
    assert "dlxt_secret_argument" not in capsys.readouterr().err
    assert main(["create", "Reader", "--expires-at", "2020-01-01T00:00:00Z"]) == 1
    assert main(["create", "Reader", "--daily-limit", "-1"]) == 1
    assert not harness.db.data["api_tokens"]


def test_auth_cache_is_bounded(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    monkeypatch.setattr("dlx_cloud.auth.AUTH_CACHE_CAPACITY", 1)
    store = harness.services.tokens
    first, key = harness.issue()
    second, _ = harness.issue()
    for token in (first, second, first):
        store.authenticate("Bearer", token)
    assert harness.db.reads["api_tokens", key] == 2


def test_token_store_requires_pepper(harness: Harness) -> None:
    with pytest.raises(ValueError, match="PEPPER"):
        TokenStore(cast(Client, harness.db), "short")
