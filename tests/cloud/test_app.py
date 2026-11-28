import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from dlx_cloud.app import create_app
from dlx_cloud.cache import segment_key
from dlx_cloud.config import CloudSettings
from dlx_tray.contracts.cloud_v1 import SourceSegment, TranslateRequest
from dlx_tray.contracts.glossary_match import matched_glossary
from dlx_tray.core.chunker import utf16_len
from dlx_tray.core.models import StylisticRegister

from .conftest import DEFAULT_REQUEST, Harness, headers


def output(*segments: tuple[int, str]) -> str:
    return json.dumps({"segments": [{"index": index, "text": text} for index, text in segments]})


def test_anonymous_cache_hits_misses_and_expiry(harness: Harness, http: TestClient) -> None:
    first, first_key = harness.issue(label="first user")
    second, second_key = harness.issue(label="second user")
    harness.sdk.queue.extend([output((3, "Translation")), output((2, "New translation"))])
    initial = http.post("/v1/translate", headers=headers(first), json=DEFAULT_REQUEST)
    assert initial.status_code == 200, initial.text
    assert initial.headers["X-Cloud-Cache"] == "miss"

    text = "Fresh \U0001f600"
    mixed = {
        **DEFAULT_REQUEST,
        "segments": [{"index": 2, "text": text}, {"index": 0, "text": "Source"}],
        "context_before": ["Private context"],
    }
    result = http.post("/v1/translate", headers=headers(second), json=mixed)
    assert result.status_code == 200, result.text
    assert result.headers["X-Cloud-Cache"] == "mixed"
    charged = utf16_len(text) + utf16_len("Private context")
    assert result.headers["X-Quota-Daily-Remaining"] == str(250_000 - charged)
    assert result.headers["X-Quota-Monthly-Remaining"] == str(3_000_000 - charged)
    assert [(s["index"], s["cached"]) for s in result.json()["segments"]] == [(2, False), (0, True)]

    final = http.post("/v1/translate", headers=headers(first), json=mixed)
    assert final.headers["X-Cloud-Cache"] == "hit"
    assert all(s["cached"] for s in final.json()["segments"])
    assert len(harness.sdk.calls) == 2
    assert json.loads(str(harness.sdk.calls[-1]["input"]))["segments"] == [
        {"index": 2, "text": text, "glossary": []}
    ]
    assert harness.services.tokens.inspect(first_key)[1] == (6, 6)
    assert harness.services.tokens.inspect(second_key)[1] == (charged, charged)
    docs = harness.db.data["translation_cache"]
    assert len(docs) == 2 and all(len(key) == 64 for key in docs)
    for doc in docs.values():
        assert set(doc) == {
            "translation",
            "detected_source",
            "notes",
            "model_tier",
            "created_at",
            "expires_at",
        }
        assert doc["created_at"] == harness.clock.now
        assert doc["expires_at"] == harness.clock.now + timedelta(days=60)
        assert all(
            secret not in repr(doc)
            for secret in ("Source", text, "Private context", first, first_key, second, second_key)
        )

    harness.clock.now += timedelta(days=60)
    harness.sdk.queue.append(output((3, "Recomputed")))
    assert (
        http.post("/v1/translate", headers=headers(first), json=DEFAULT_REQUEST).headers[
            "X-Cloud-Cache"
        ]
        == "miss"
    )
    assert len(harness.sdk.calls) == 3


def test_cache_key_scope() -> None:
    segment = SourceSegment(index=3, text="Magic Source")
    fields: dict[str, Any] = {
        "segments": [segment],
        "source_lang": "EN",
        "target_lang": "UK",
        "style_context": " era ",
        "glossary": [
            {"source_term": "MAGIC", "target_term": "Target", "priority": 2},
            {"source_term": "magic", "target_term": "Ignored priority"},
        ],
    }

    def key(model: str = "model", **changes: object) -> str:
        request = TranslateRequest.model_validate(fields | changes)
        return segment_key(
            request, segment, model, matched_glossary(segment.text, request.glossary)
        )

    baseline = key()
    assert key(context_before=["Other context"], style_context="era") == baseline
    assert key(glossary=[{"source_term": "magic", "target_term": "Target"}]) == baseline
    for changed in (
        key("other-model"),
        key(analysis_tier="deep"),
        key(source_lang="auto"),
        key(target_lang="FR"),
        key(style_context="period"),
        key(register="literal"),
        key(include_notes=True),
        key(glossary=[{"source_term": "magic", "target_term": "Different"}]),
    ):
        assert changed != baseline


@pytest.mark.parametrize("style", list(StylisticRegister))
def test_style_prompt_and_notes(
    harness: Harness, http: TestClient, style: StylisticRegister
) -> None:
    token, _ = harness.issue(allow_deep_tier=True)
    harness.sdk.queue.append(
        json.dumps(
            {
                "segments": [
                    {
                        "index": 3,
                        "text": "Translation",
                        "detected_source": "EN",
                        "notes": [{"kind": "idiom", "text": "An explanation"}],
                        "cached": True,
                    }
                ]
            }
        )
    )
    payload = {
        **DEFAULT_REQUEST,
        "register": style,
        "style_context": " middle ages ",
        "analysis_tier": "deep",
        "include_notes": True,
        "glossary": [
            {"source_term": "Source", "target_term": "Glossary target"},
            {"source_term": "unmatched", "target_term": "Do not forward"},
        ],
    }
    response = http.post("/v1/translate", json=payload, headers=headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["notes"] == [{"kind": "idiom", "text": "An explanation"}]
    assert response.json()["segments"][0]["cached"] is False
    call = harness.sdk.calls[0]
    assert call["model"] == "gemini-4-neon"
    assert call["store"] is False
    assert call["generation_config"] == {"thinking_level": "high", "max_output_tokens": 65_536}
    assert isinstance(call["response_format"], dict)
    assert "segments" in call["response_format"]["schema"]["properties"]
    prompt = json.loads(str(call["input"]))
    assert prompt["register"] == style
    assert prompt["register_guidance"]
    assert prompt["style_context"] == "middle ages"
    assert prompt["segments"][0]["glossary"] == [["source", "Glossary target"]]

    other, _ = harness.issue()
    forbidden = http.post("/v1/translate", json=payload, headers=headers(other))
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "deep_tier_forbidden"
    assert len(harness.sdk.calls) == 1


def test_extract_terms_uses_deep_tier_and_never_caches(harness: Harness, http: TestClient) -> None:
    request = {"segments": ["Source term", "\U0001f600"], "target_lang": "UK"}
    denied, _ = harness.issue()
    response = http.post("/v1/extract-terms", json=request, headers=headers(denied))
    assert response.status_code == 403
    assert not harness.sdk.calls

    token, key = harness.issue(allow_deep_tier=True)
    terms = {
        "terms": [{"source_term": "Term", "target_term": "Concept", "category": "noun", "count": 2}]
    }
    harness.sdk.queue.append(json.dumps(terms))
    result = http.post("/v1/extract-terms", json=request, headers=headers(token))
    assert result.status_code == 200
    assert result.json()["terms"][0] == {**terms["terms"][0], "note": None}
    assert "X-Cloud-Cache" not in result.headers
    assert harness.services.tokens.inspect(key)[1] == (13, 13)
    assert harness.sdk.calls[0]["model"] == "gemini-4-neon"
    assert harness.sdk.calls[0]["store"] is False
    assert not harness.db.data["translation_cache"]


@pytest.mark.parametrize(
    "malformed", [None, "not JSON", output((7, "Wrong index")), output((3, " "))]
)
def test_malformed_output_retried_once_and_charged_per_dispatch(
    harness: Harness, http: TestClient, malformed: str | None
) -> None:
    token, key = harness.issue()
    harness.sdk.queue.extend([malformed, output((3, "Corrected"))])
    response = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert response.status_code == 200
    assert response.json()["segments"][0]["text"] == "Corrected"
    assert harness.services.tokens.inspect(key)[1] == (12, 12)
    assert len(harness.sdk.calls) == 2


def test_repeated_malformed_output_is_not_cached(harness: Harness, http: TestClient) -> None:
    token, _ = harness.issue()
    harness.sdk.queue.extend(["{", "}"])
    failed = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert failed.status_code == 503
    assert failed.json()["code"] == "upstream_unavailable"
    assert not harness.db.data["translation_cache"]


@pytest.mark.parametrize("status", [429, 503])
def test_upstream_error_mapping_and_redaction(
    harness: Harness, http: TestClient, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    token, _ = harness.issue()
    cls = errors.ClientError if status == 429 else errors.ServerError
    private = "Sensitive user source and glossary"
    harness.sdk.queue.append(cls(status, {"error": {"message": f"{private} {token}"}}))
    response = http.post(
        "/v1/translate",
        json={"segments": [{"index": 1, "text": private}], "target_lang": "UK"},
        headers=headers(token),
    )
    assert response.status_code == status
    assert response.json()["code"] == ("rate_limited" if status == 429 else "upstream_unavailable")
    assert private not in response.text + caplog.text
    assert token not in response.text + caplog.text
    assert len(harness.sdk.calls) == 1


def test_quota_gate_applies_to_retry_but_not_cache_hits(harness: Harness, http: TestClient) -> None:
    token, key = harness.issue(daily_char_limit=6)
    harness.sdk.queue.append("malformed")
    result = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert result.status_code == 429
    assert result.json()["code"] == "quota_exceeded"
    assert len(harness.sdk.calls) == 1
    assert harness.services.tokens.inspect(key)[1] == (6, 6)

    other, _ = harness.issue()
    harness.sdk.queue.append(output((3, "Cached result")))
    assert (
        http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(other)).status_code == 200
    )
    result = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert result.status_code == 200
    assert result.headers["X-Cloud-Cache"] == "hit"
    assert result.headers["X-Quota-Daily-Remaining"] == "0"
    assert len(harness.sdk.calls) == 2


def test_expanded_glossary_input_is_limited_and_metered(harness: Harness, http: TestClient) -> None:
    token, key = harness.issue(daily_char_limit=1)
    payload = {
        "target_lang": "UK",
        "segments": [{"index": 0, "text": "S"}],
        "glossary": [{"source_term": "S", "target_term": "X" * 300_000}],
    }
    result = http.post("/v1/translate", json=payload, headers=headers(token))
    assert result.status_code == 429
    assert result.json()["code"] == "quota_exceeded"

    payload["segments"].append({"index": 1, "text": "Second S"})
    result = http.post("/v1/translate", json=payload, headers=headers(token))
    assert result.status_code == 413
    assert result.json()["code"] == "payload_too_large"
    assert harness.services.tokens.inspect(key)[1] == (0, 0)
    assert not harness.sdk.calls


@pytest.mark.parametrize("path", ["/v1/translate", "/v1/extract-terms"])
def test_body_limits_for_declared_and_streamed_payloads(harness: Harness, path: str) -> None:
    settings = CloudSettings(max_body_size=64)
    token, _ = harness.issue()
    with TestClient(create_app(settings, harness.services)) as client:
        for body in (b"x" * 100, iter((b"x" * 40, b"x" * 60))):
            response = client.post(
                path, content=body, headers={**headers(token), "Content-Type": "application/json"}
            )
            assert response.status_code == 413
            assert response.json()["code"] == "payload_too_large"
    assert not harness.sdk.calls
    assert not harness.db.data["translation_cache"]


def test_bad_request_is_redacted(harness: Harness, http: TestClient) -> None:
    token, _ = harness.issue()
    response = http.post("/v1/translate", json={"private": "Sensitive"}, headers=headers(token))
    assert response.status_code == 422
    assert "Sensitive" not in response.text
    assert not harness.sdk.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"style_context": "\ud800"},
        {"segments": [{"index": 3, "text": "\ud800"}]},
    ],
)
def test_lone_surrogates_are_rejected(
    harness: Harness, http: TestClient, changes: dict[str, object]
) -> None:
    token, _ = harness.issue()
    response = http.post(
        "/v1/translate",
        content=json.dumps(DEFAULT_REQUEST | changes),
        headers={**headers(token), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert not harness.sdk.calls


def test_verbatim_source_echo_is_returned_without_persisting(
    harness: Harness, http: TestClient
) -> None:
    token, _ = harness.issue()
    harness.sdk.queue.extend(
        [
            output((3, "Source")),
            json.dumps(
                {
                    "segments": [
                        {
                            "index": 3,
                            "text": "Translated",
                            "notes": [{"kind": "grammar", "text": "Details about Source"}],
                        }
                    ]
                }
            ),
        ]
    )
    for options in ({}, {"include_notes": True}):
        response = http.post(
            "/v1/translate", json=DEFAULT_REQUEST | options, headers=headers(token)
        )
        assert response.status_code == 200
    assert not harness.db.data["translation_cache"]


@pytest.mark.parametrize(
    "private",
    ["A private paragraph", "A neighbouring segment", "Private glossary target"],
)
def test_mixed_batch_echo_does_not_leak_to_another_token(
    harness: Harness, http: TestClient, private: str
) -> None:
    first, _ = harness.issue()
    second, _ = harness.issue()
    harness.sdk.queue.extend(
        [
            output((1, "Previous translation")),
            json.dumps(
                {
                    "segments": [
                        {
                            "index": 3,
                            "text": "Initial translation",
                            "notes": [{"kind": "grammar", "text": f"Details about {private}"}],
                        },
                        {"index": 4, "text": "Private glossary target"},
                    ]
                }
            ),
            output((3, "Independent translation")),
        ]
    )
    cached_segment = {"index": 1, "text": "Previously translated"}
    anchor = {"index": 3, "text": "Shared anchor"}
    options = {"target_lang": "UK", "include_notes": True}
    warm = http.post(
        "/v1/translate", headers=headers(first), json={**options, "segments": [cached_segment]}
    )
    assert warm.status_code == 200
    payload = {
        **options,
        "segments": [
            cached_segment,
            anchor,
            {"index": 4, "text": "A neighbouring segment"},
        ],
        "context_before": ["A private paragraph"],
        "glossary": [
            {"source_term": "neighbouring segment", "target_term": "Private glossary target"}
        ],
    }
    initial = http.post("/v1/translate", headers=headers(first), json=payload)
    assert initial.status_code == 200
    assert initial.headers["X-Cloud-Cache"] == "mixed"
    assert private in initial.json()["segments"][1]["notes"][0]["text"]
    assert len(harness.db.data["translation_cache"]) == 2
    response = http.post(
        "/v1/translate", headers=headers(second), json={**options, "segments": [anchor]}
    )
    assert response.status_code == 200
    assert response.headers["X-Cloud-Cache"] == "miss"
    assert response.json()["segments"][0]["text"] == "Independent translation"
    assert private not in response.text


def test_cache_faults_are_safe_misses(
    harness: Harness, http: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    token, _ = harness.issue()
    harness.db.read_errors.add("translation_cache")
    harness.db.write_errors.add("translation_cache")
    harness.sdk.queue.extend([output((3, "Translation")), output((3, "Fresh"))])
    response = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert response.status_code == 200
    assert not harness.db.data["translation_cache"]
    harness.db.read_errors.clear()
    harness.db.write_errors.clear()
    request = TranslateRequest.model_validate(DEFAULT_REQUEST)
    key = segment_key(request, request.segments[0], harness.settings.model_standard, ())
    harness.db.data["translation_cache"][key] = {
        "translation": [],
        "expires_at": harness.clock.now + timedelta(days=1),
    }
    response = http.post("/v1/translate", json=DEFAULT_REQUEST, headers=headers(token))
    assert response.json()["segments"][0]["text"] == "Fresh"
    assert "private upstream error" not in caplog.text


def test_unconfigured_service_can_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLX_CLOUD_TOKEN_PEPPER", raising=False)
    monkeypatch.delenv("DLX_CLOUD_GEMINI_API_KEY", raising=False)
    with TestClient(create_app(CloudSettings())) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/me").status_code == 503
        assert client.get("/unknown").status_code == 404
