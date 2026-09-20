"""HTTP inference provider (D11).

No test in this module talks to the internet: the transport-dependent behaviour
is exercised against a mock HTTP server bound to ``127.0.0.1``, and everything
else against an injected transport. The properties pinned here are the ones the
pipeline's honesty rests on — an authenticated JSON chat-completion request, the
configured endpoint/model only, bounded retries, no secret in any error text, and
an explicit failure (never a fabricated payload) for every bad response.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from boardmodeler.config import ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.providers.base import (
    DocSnippet,
    ExtractionRequest,
    ExtractionTask,
    ProviderError,
)
from boardmodeler.providers.http_inference import (
    HttpInferenceProvider,
    HttpRequest,
    HttpResponse,
    build_chat_body,
)
from boardmodeler.security import credentials

NAME = "http_inference"
ENV_VAR = "BOARDMODELER_HTTP_INFERENCE_API_KEY"
SECRET = "unit-test-secret-value"
MODEL = "documented-model-name"
SNIPPET_TEXT = "The input range is 4.5 V to 60 V."
PAGE_LABEL = "Electrical Characteristics"


# --------------------------------------------------------------------------- #
# mock endpoint


class MockEndpoint:
    """A loopback-only HTTP server that records requests and replays canned answers."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.queue: list[tuple[int, bytes]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1/chat/completions"

    @property
    def hits(self) -> int:
        return len(self.requests)

    def enqueue(self, status: int, payload: dict[str, Any] | str) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self.queue.append((status, body.encode("utf-8")))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _respond(self, request: BaseHTTPRequestHandler, body: bytes) -> None:
        self.requests.append(
            {
                "method": request.command,
                "path": request.path,
                "headers": {str(key): str(value) for key, value in request.headers.items()},
                "body": body,
            }
        )
        status, payload = self.queue.pop(0) if self.queue else (200, chat_response({"ok": True}))
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)


def _handler_for(endpoint: MockEndpoint) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            endpoint._respond(self, b"")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            endpoint._respond(self, self.rfile.read(length))

        def log_message(self, *args: object) -> None:
            """Silence the stdlib request log."""

    return Handler


def chat_response(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode("utf-8")


@pytest.fixture
def endpoint() -> MockEndpoint:
    server = MockEndpoint()
    try:
        yield server
    finally:
        server.close()


class EmptyKeyring:
    """A keyring with no entries, so only the environment can supply a credential."""

    def get_password(self, service: str, key: str) -> str | None:
        return None


@pytest.fixture
def credential(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(credentials, "keyring", EmptyKeyring())
    monkeypatch.setenv(ENV_VAR, SECRET)
    return SECRET


@pytest.fixture
def no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "keyring", EmptyKeyring())
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BOB_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# helpers


def make_provider(
    url: str | None,
    *,
    transport: Callable[[HttpRequest], HttpResponse] | None = None,
    **overrides: Any,
) -> HttpInferenceProvider:
    fields: dict[str, Any] = {
        "kind": ProviderKind.HTTP_INFERENCE,
        "endpoint": url,
        "model": MODEL,
        "timeout_s": 10.0,
        "retries": 0,
    }
    fields.update(overrides)
    return HttpInferenceProvider(
        provider_config=ProviderConfig(**fields),
        name=NAME,
        transport=transport,
        sleep=lambda _seconds: None,
    )


def make_request(
    *,
    task: ExtractionTask = ExtractionTask.REQUIREMENTS,
    prompt: str = "extract the limits",
    snippet_text: str = SNIPPET_TEXT,
) -> ExtractionRequest:
    return ExtractionRequest(
        task=task,
        prompt=prompt,
        schema_json='{"type": "object"}',
        snippets=(
            DocSnippet(
                doc_id="DOC_1",
                pdf_page=3,
                printed_label=PAGE_LABEL,
                text=snippet_text,
            ),
        ),
    )


def sent_body(endpoint: MockEndpoint) -> dict[str, Any]:
    return json.loads(endpoint.requests[-1]["body"].decode("utf-8"))


# --------------------------------------------------------------------------- #
# request shape


def test_extract_posts_an_authenticated_chat_completion(
    endpoint: MockEndpoint, credential: str
) -> None:
    provider = make_provider(endpoint.url)
    response = provider.extract(make_request())

    assert endpoint.hits == 1
    sent = endpoint.requests[-1]
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"].get("Authorization") == f"Bearer {SECRET}"
    assert sent["headers"].get("Content-Type") == "application/json"

    body = sent_body(endpoint)
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    user_content = body["messages"][1]["content"]
    assert "extract the limits" in user_content
    assert SNIPPET_TEXT in user_content
    assert f"page 3 (printed {PAGE_LABEL})" in user_content

    assert response.payload == {"ok": True}
    assert response.from_cache is False
    assert response.identity.kind is ProviderKind.HTTP_INFERENCE
    assert response.identity.model == MODEL
    assert response.identity.endpoint == endpoint.url
    assert response.identity.usage_units == "tokens"
    assert response.identity.usage == {
        "prompt_tokens": 10.0,
        "completion_tokens": 5.0,
        "total_tokens": 15.0,
    }


def test_configured_limits_and_temperature_are_forwarded(
    endpoint: MockEndpoint, credential: str
) -> None:
    provider = make_provider(endpoint.url, max_output_tokens=123, temperature=0.25)
    provider.extract(make_request())
    body = sent_body(endpoint)
    assert body["max_tokens"] == 123
    assert body["temperature"] == 0.25


def test_auth_header_and_scheme_come_from_configuration(
    endpoint: MockEndpoint, credential: str
) -> None:
    provider = make_provider(endpoint.url, auth_header="X-Api-Key", auth_scheme="")
    provider.extract(make_request())
    headers = endpoint.requests[-1]["headers"]
    assert headers.get("X-Api-Key") == SECRET
    assert "Authorization" not in headers


def test_build_chat_body_is_deterministic_and_rejects_an_empty_model() -> None:
    snippets = (DocSnippet(doc_id="D", pdf_page=0, printed_label=None, text="text"),)
    first = build_chat_body(model=MODEL, prompt="p", snippets=snippets, max_output_tokens=10)
    second = build_chat_body(model=MODEL, prompt="p", snippets=snippets, max_output_tokens=10)
    assert first == second
    with pytest.raises(ValueError):
        build_chat_body(model="", prompt="p", snippets=snippets, max_output_tokens=10)


# --------------------------------------------------------------------------- #
# failure handling


def test_missing_credential_sends_nothing(endpoint: MockEndpoint, no_credential: None) -> None:
    provider = make_provider(endpoint.url)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "credential_missing"
    assert ENV_VAR in excinfo.value.detail
    assert endpoint.hits == 0
    assert SECRET not in excinfo.value.detail


def test_unconfigured_endpoint_and_model_are_explicit(
    endpoint: MockEndpoint, credential: str
) -> None:
    with pytest.raises(ProviderError) as endpoint_error:
        make_provider(None).extract(make_request())
    assert endpoint_error.value.code == "endpoint_not_configured"
    assert endpoint.hits == 0

    with pytest.raises(ProviderError) as model_error:
        make_provider(endpoint.url, model=None).extract(make_request())
    assert model_error.value.code == "model_not_configured"
    assert endpoint.hits == 0

    health = make_provider(None).health(1.0)
    assert health.ok is False
    assert health.code == "endpoint_not_configured"
    assert endpoint.hits == 0


def test_retries_a_failing_server_then_succeeds(endpoint: MockEndpoint, credential: str) -> None:
    endpoint.enqueue(503, {"error": "try again"})
    endpoint.enqueue(500, {"error": "still failing"})
    provider = make_provider(endpoint.url, retries=2)
    response = provider.extract(make_request())
    assert response.payload == {"ok": True}
    assert endpoint.hits == 3


def test_retries_are_bounded(endpoint: MockEndpoint, credential: str) -> None:
    for _ in range(5):
        endpoint.enqueue(500, {"error": "down"})
    provider = make_provider(endpoint.url, retries=1)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "http_error"
    assert endpoint.hits == 2
    assert "after 2 of 2 attempts" in excinfo.value.detail


def test_auth_error_is_not_retried(endpoint: MockEndpoint, credential: str) -> None:
    endpoint.enqueue(401, {"error": "bad key"})
    endpoint.enqueue(200, {"ok": True})
    provider = make_provider(endpoint.url, retries=3)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "http_auth_error"
    assert endpoint.hits == 1


def test_secret_is_redacted_from_error_text(endpoint: MockEndpoint, credential: str) -> None:
    endpoint.enqueue(500, {"error": f"the key 'Bearer {SECRET}' is not valid"})
    provider = make_provider(endpoint.url, retries=0)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert SECRET not in excinfo.value.detail
    assert "[REDACTED]" in excinfo.value.detail


def test_non_json_assistant_message_is_rejected(endpoint: MockEndpoint, credential: str) -> None:
    endpoint.enqueue(200, "not json at all")
    provider = make_provider(endpoint.url)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "response_not_json"


def test_a_fenced_json_object_is_accepted(endpoint: MockEndpoint, credential: str) -> None:
    content = '```json\n{"ok": true}\n```'
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
    endpoint.enqueue(200, body.decode("utf-8"))
    provider = make_provider(endpoint.url)
    assert provider.extract(make_request()).payload == {"ok": True}


def test_a_response_without_choices_is_rejected(endpoint: MockEndpoint, credential: str) -> None:
    endpoint.enqueue(200, {"ok": "no choices here"})
    provider = make_provider(endpoint.url)
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "response_no_choices"


def test_cancellation_before_the_call_sends_nothing(
    endpoint: MockEndpoint, credential: str
) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderError) as excinfo:
        make_provider(endpoint.url).extract(make_request(), cancel)
    assert excinfo.value.code == "cancelled"
    assert endpoint.hits == 0


# --------------------------------------------------------------------------- #
# health


def test_health_accepts_a_reachable_endpoint(endpoint: MockEndpoint, credential: str) -> None:
    health = make_provider(endpoint.url).health(5.0)
    assert health.ok is True
    assert health.code == "ok"
    assert health.latency_ms is not None
    assert endpoint.hits == 1, "the probe is one GET, never an inference call"
    assert endpoint.requests[-1]["method"] == "GET"


def test_health_reports_an_unreachable_endpoint(credential: str) -> None:
    def broken(request: HttpRequest) -> HttpResponse:
        raise OSError("connection refused")

    health = make_provider("http://127.0.0.1:1/v1/chat/completions", transport=broken).health(1.0)
    assert health.ok is False
    assert health.code == "endpoint_unreachable"
    assert "connection refused" in health.detail


def test_health_requires_a_credential(endpoint: MockEndpoint, no_credential: None) -> None:
    health = make_provider(endpoint.url).health(1.0)
    assert health.ok is False
    assert health.code == "credential_missing"
    assert endpoint.hits == 0


# --------------------------------------------------------------------------- #
# endpoint strings are never invented (D-005)
