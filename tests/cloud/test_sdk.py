import json
from collections import deque

import httpx2
import pytest
from fastapi.testclient import TestClient
from google import genai
from google.genai.types import HttpOptions
from pydantic import SecretStr

from dlx_cloud.app import create_app
from dlx_cloud.config import CloudSettings

from .conftest import DEFAULT_REQUEST, PEPPER, Harness, headers


def test_gemini_sdk_wire_and_startup(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    issued, key = harness.issue()
    calls: list[dict] = []
    statuses = deque((429, 503, 200, 200))

    def upstream(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/interactions"
        assert request.headers["x-goog-api-key"] == "test-api-key"
        calls.append(json.loads(request.content))
        status = statuses.popleft()
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": f"private error {issued}"}})
        if statuses:
            return httpx2.Response(200, text="Malformed upstream body")
        text = json.dumps({"segments": [{"index": 3, "text": "Wire translation"}]})
        return httpx2.Response(
            200,
            json={
                "status": "completed",
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
            },
        )

    real_client = genai.Client

    def mock_transport(*, api_key: str, vertexai: bool, http_options: HttpOptions) -> genai.Client:
        http_options.httpx_client = httpx2.Client(transport=httpx2.MockTransport(upstream))
        return real_client(api_key=api_key, vertexai=vertexai, http_options=http_options)

    monkeypatch.setattr(CloudSettings, "firestore", lambda _: harness.db)
    monkeypatch.setattr(genai, "Client", mock_transport)
    settings = CloudSettings(
        token_pepper=SecretStr(PEPPER),
        gemini_api_key=SecretStr("test-api-key"),
        model_standard="gemini-4-hydrogen-test",
    )
    with TestClient(create_app(settings)) as http:
        for status in (429, 503, 200):
            response = http.post("/v1/translate", headers=headers(issued), json=DEFAULT_REQUEST)
            assert response.status_code == status, response.text
        assert response.json()["segments"][0]["text"] == "Wire translation"
    assert harness.db.closes == 1
    assert len(calls) == 4 and not statuses
    assert harness.services.tokens.inspect(key)[1] == (24, 24)
    assert issued not in caplog.text
    for body in calls:
        assert body["model"] == "gemini-4-hydrogen-test"
        assert body["store"] is False
        assert body["generation_config"] == {"thinking_level": "low", "max_output_tokens": 32_768}
        assert body["response_format"]["mime_type"] == "application/json"
        assert body["response_format"]["schema"]["properties"]["segments"]["type"] == "array"
        assert "response_mime_type" not in body and "tools" not in body
