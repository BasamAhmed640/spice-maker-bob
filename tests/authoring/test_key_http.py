from __future__ import annotations

import json

import pytest

from boardmodeler.agent_providers import require
from boardmodeler.authoring.api_backend import verify_http_key
from boardmodeler.providers.http_inference import HttpResponse, _NoCredentialRedirect

SECRET = "test-key-not-for-logs"


@pytest.mark.parametrize("provider_id", ["opencode_go", "anthropic", "google"])
def test_small_real_inference_shape_keeps_model_and_reasoning(provider_id):
    provider = require(provider_id)

    def transport(request):
        body = json.loads(request.body)
        assert SECRET not in request.url and SECRET not in request.body.decode()
        assert any(SECRET in v for v in request.headers.values())
        assert request.headers["User-Agent"].startswith("SpiceMaker/")
        assert request.timeout_s == 15
        if provider_id == "opencode_go":
            assert body["model"] == "chosen-model"
            assert body["max_tokens"] == 256
            assert body["reasoning_effort"] == "max"
            payload = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        elif provider_id == "anthropic":
            assert body["model"] == "chosen-model"
            assert body["max_tokens"] == 256
            assert body["output_config"]["effort"] == "max"
            payload = {"type": "message", "content": [], "stop_reason": "max_tokens"}
        else:
            assert "chosen-model" in request.url
            assert body["generationConfig"]["maxOutputTokens"] == 256
            assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"
            payload = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
        return HttpResponse(200, {}, json.dumps(payload).encode())

    result = verify_http_key(provider, SECRET, model="chosen-model", transport=transport)
    assert result.status == "verified"


@pytest.mark.parametrize(
    "http_status,status",
    [
        (401, "rejected"),
        (403, "rejected"),
        (429, "unverified"),
        (500, "unverified"),
        (302, "unverified"),
        (200, "unverified"),
    ],
)
def test_failure_never_echoes_secret_or_awards_false_success(http_status, status):
    calls = []

    def transport(request):
        calls.append(request)
        return HttpResponse(http_status, {}, json.dumps({"error": SECRET}).encode())

    result = verify_http_key(require("opencode_go"), SECRET, transport=transport)
    assert result.status == status
    assert SECRET not in repr(result)
    assert len(calls) == 1


def test_timeout_is_not_an_invalid_key_and_redirects_are_refused():
    def transport(request):
        raise TimeoutError(SECRET)

    result = verify_http_key(require("opencode_go"), SECRET, transport=transport)
    assert result.status == "unverified" and SECRET not in repr(result)
    assert (
        _NoCredentialRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        is None
    )
