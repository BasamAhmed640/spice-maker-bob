"""The API-key author backend: one request, strict files, no secret in any text.

Everything here is offline: the transport is injected, the credential lookup is
injected, and no test reaches a network endpoint or the OS keyring. What the
tests pin is the contract the pipeline and the GUI rely on — which files land on
disk after a reply, which replies are refused, what ``availability`` says when a
key is missing, and that a server echoing the key back cannot leak it.

The subject is the *backend*, not the catalog: every wire, path-safety, redaction
and budget test speaks through the entry this build ships for that wire, or — in a
build that ships none — through the documented shape :data:`WIRE_ENTRIES` declares.
The catalog checks the backend performs are covered by the factory tests below,
which read whatever this build accepts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from boardmodeler import agent_providers
from boardmodeler.agent_providers import CATALOG, AgentProvider, by_id, ids
from boardmodeler.authoring import api_backend
from boardmodeler.authoring.api_backend import ApiKeyBackend, build_api_backend
from boardmodeler.authoring.backends import (
    AuthorRequest,
    AuthorResult,
    BobShellBackend,
    UnavailableBackend,
)
from boardmodeler.config import AppConfig
from boardmodeler.providers.http_inference import HttpRequest, HttpResponse
from boardmodeler.security.credentials import (
    REDACTED,
    Credential,
    SecretSource,
    env_var_name,
)

SECRET = "sk-test-key-3f9a1c7d"
SUBCKT = "BM_REG_BUCK"
LIB_TEXT = f".subckt {SUBCKT} VIN SW FB GND\nR1 VIN FB 1k\n.ends {SUBCKT}\n"
ASY_TEXT = "Version 4\nSymbolType CELL\n"

#: The wire shapes this module exercises, declared here so the backend tests run in a
#: build that ships none of them. Each is a copy of the entry the general build ships
#: (``test_the_declared_wire_shapes_match_the_entries_this_build_ships`` holds them
#: together); :func:`provider` prefers the build's own entry whenever it has one.
WIRE_ENTRIES: tuple[AgentProvider, ...] = (
    AgentProvider(
        id="deepseek",
        label="DeepSeek",
        wire="openai",
        credential="deepseek",
        key_label="DEEPSEEK API KEY",
        key_hint="platform.deepseek.com → API keys  ·  stored in the Windows credential store",
        docs="https://api-docs.deepseek.com/",
        endpoint="https://api.deepseek.com",
        model="deepseek-flash",
        env_aliases=("DEEPSEEK_API_KEY",),
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        retry_body={"thinking": {"type": "disabled"}},
    ),
    AgentProvider(
        id="openai",
        label="OpenAI",
        wire="openai",
        credential="openai",
        key_label="OPENAI API KEY",
        key_hint="platform.openai.com → API keys  ·  stored in the Windows credential store",
        docs="https://developers.openai.com/api/docs/guides/text",
        endpoint="https://api.openai.com/v1",
        model="gpt-6-astra",
        env_aliases=("OPENAI_API_KEY",),
        reasoning=True,
    ),
    AgentProvider(
        id="anthropic",
        label="Anthropic",
        wire="anthropic",
        credential="anthropic",
        key_label="ANTHROPIC API KEY",
        key_hint="console.anthropic.com → API keys  ·  stored in the Windows credential store",
        docs="https://platform.claude.com/docs/en/get-started",
        endpoint="https://api.anthropic.com/v1",
        model="claude-opus-5",
        env_aliases=("ANTHROPIC_API_KEY",),
    ),
    AgentProvider(
        id="google",
        label="Google Gemini",
        wire="google",
        credential="google",
        key_label="GEMINI API KEY",
        key_hint="aistudio.google.com → API keys  ·  stored in the Windows credential store",
        docs="https://ai.google.dev/gemini-api/docs/text-generation",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-3.8-flash",
        env_aliases=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
)

OPENAI = by_id("openai") or WIRE_ENTRIES[1]
DEEPSEEK = by_id("deepseek") or WIRE_ENTRIES[0]
ANTHROPIC = by_id("anthropic") or WIRE_ENTRIES[2]
GOOGLE = by_id("google") or WIRE_ENTRIES[3]


def provider(wire_id: str) -> AgentProvider:
    """The entry this build ships for ``wire_id``, or the declared shape for that wire."""
    return by_id(wire_id) or next(entry for entry in WIRE_ENTRIES if entry.id == wire_id)


@pytest.fixture(autouse=True)
def accepts_wire_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this build accept the wire entries the tests speak through.

    ``ApiKeyBackend`` refuses an id its build does not accept, so without this a
    restricted build would answer every wire test with that refusal. Only entries the
    build lacks are added, and ``monkeypatch`` removes them again afterwards: the
    catalog the build ships is never swapped, so the tests that read it still see it.
    """
    declared = tuple(entry for entry in WIRE_ENTRIES if by_id(entry.id) is None)
    if declared:
        monkeypatch.setattr(agent_providers, "CATALOG", (*agent_providers.CATALOG, *declared))


@pytest.fixture(autouse=True)
def no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read a developer's own exported key, whatever it is called."""
    for entry in (*CATALOG, *WIRE_ENTRIES):
        monkeypatch.delenv(env_var_name(entry.credential), raising=False)
        for alias in entry.env_aliases:
            monkeypatch.delenv(alias, raising=False)


def test_the_declared_wire_shapes_match_the_entries_this_build_ships() -> None:
    """Where this build ships a declared id, it must ship the shape these tests speak."""
    for declared in WIRE_ENTRIES:
        shipped = by_id(declared.id)
        if shipped is not None:
            assert shipped == declared, f"{declared.id} differs from the shape used here"


class Recorder:
    """An injected transport: records every request, answers with one canned body."""

    def __init__(self, body: str | bytes, *, status: int = 200) -> None:
        self.payload = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(status=self.status, headers={}, body=self.payload)


class Sequenced(Recorder):
    """Answers with each queued response in turn (for retry tests)."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        super().__init__("")
        self.queue = list(responses)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        status, body = self.queue.pop(0)
        return HttpResponse(status=status, headers={}, body=body.encode("utf-8"))


def with_key(value: str = SECRET):
    def lookup(name: str) -> Credential:
        return Credential(
            name=name, value=value, source=SecretSource.KEYRING, detail="keyring service='x'"
        )

    return lookup


def without_key():
    def lookup(name: str) -> Credential:
        return Credential(
            name=name, value=None, source=SecretSource.MISSING, detail=f"no value for {name}"
        )

    return lookup


def openai_reply(text: str, *, finish_reason: str = "stop") -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
    )


def request_for(tmp_path: Path, *, expect_text: bool = False) -> AuthorRequest:
    workdir = tmp_path / "build"
    model_dir = workdir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    return AuthorRequest(
        prompt="author the model",
        workdir=workdir,
        model_dir=model_dir,
        max_turns=12,
        expect_text=expect_text,
    )


def written_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def backend_for(entry: AgentProvider, transport: Recorder, **overrides: Any) -> ApiKeyBackend:
    values: dict[str, Any] = {"transport": transport, "credential_lookup": with_key()}
    values.update(overrides)
    return ApiKeyBackend(entry, **values)


# --------------------------------------------------------------------------- #
# the OpenAI wire: a reply becomes exactly the model files


def test_an_openai_reply_writes_the_model_files_and_nothing_else(tmp_path: Path) -> None:
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT, f"{SUBCKT}.asy": ASY_TEXT}})
    transport = Recorder(openai_reply(reply))
    schema_holder = provider("openai")
    backend = backend_for(schema_holder, transport)
    request = request_for(tmp_path)

    result = backend.author(request)

    assert result.ok is True, result.detail
    assert (request.model_dir / f"{SUBCKT}.lib").read_text(encoding="utf-8") == LIB_TEXT
    assert (request.model_dir / f"{SUBCKT}.asy").read_text(encoding="utf-8") == ASY_TEXT
    assert written_files(request.workdir) == [f"model/{SUBCKT}.asy", f"model/{SUBCKT}.lib"]
    assert result.stdout_tail and result.usage["prompt_tokens"] == 11.0

    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url == f"{schema_holder.endpoint}/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {SECRET}"
    body = json.loads(sent.body)
    assert body["model"] == schema_holder.model
    assert body["stream"] is False
    assert "temperature" not in body and "max_tokens" not in body
    assert body["max_completion_tokens"] == api_backend.MAX_OUTPUT_TOKENS
    assert body["messages"][0]["role"] == "user"
    assert "files" in body["messages"][0]["content"], "the reply shape must be requested"
    assert SECRET not in sent.headers.get("Content-Type", "")


def test_a_providers_own_request_switch_reaches_the_body(tmp_path: Path) -> None:
    """A vendor knob the entry declares must be in the request it sends.

    DeepSeek's ``thinking``/``reasoning_effort`` switch is the difference between its
    model answering and it spending the whole budget on reasoning tokens with no text
    at all, so this is not cosmetic: the shipped entry's switch must arrive. Low effort
    is the setting that was measured to produce a model the harness can judge (0 PASS
    with thinking off, 4 PASS / 0 FAIL with it on at low effort) — the values live in
    ``agent_providers`` and this test only proves they are sent.
    """
    deepseek = provider("deepseek")
    assert deepseek.extra_body, "the DeepSeek entry declares its documented switch"
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))

    result = backend_for(deepseek, transport).author(request_for(tmp_path))

    assert result.ok is True, result.detail
    body = json.loads(transport.requests[0].body)
    assert body["thinking"] == deepseek.extra_body["thinking"]
    assert body["reasoning_effort"] == deepseek.extra_body["reasoning_effort"] == "low"


def test_an_entry_without_a_switch_sends_none(tmp_path: Path) -> None:
    """The mechanism is per entry: a provider that declares nothing adds nothing."""
    plain = provider("openai")
    assert not plain.extra_body, "this entry declares no vendor switch"
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))

    result = backend_for(plain, transport).author(request_for(tmp_path))

    assert result.ok is True, result.detail
    body = json.loads(transport.requests[0].body)
    assert "thinking" not in body


def test_an_empty_reply_at_the_budget_is_reasked_with_the_fallback_setting(
    tmp_path: Path,
) -> None:
    """A model that thinks the whole budget away gets the entry's second setting.

    This is the failure that actually ends a DeepSeek turn: the answer exists, but the
    reply is empty at ``finish_reason='length'``. The entry declares the setting that
    makes the model answer, so the turn is re-asked instead of lost.
    """
    deepseek = provider("deepseek")
    assert deepseek.retry_body, "the DeepSeek entry declares its fallback setting"
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT}})
    transport = Sequenced(
        (200, openai_reply("", finish_reason="length")), (200, openai_reply(reply))
    )
    request = request_for(tmp_path)

    result = backend_for(deepseek, transport).author(request)

    assert result.ok is True, result.detail
    assert (request.model_dir / f"{SUBCKT}.lib").read_text(encoding="utf-8") == LIB_TEXT
    first = json.loads(transport.requests[0].body)
    second = json.loads(transport.requests[1].body)
    assert first["thinking"] == deepseek.extra_body["thinking"]
    assert first["reasoning_effort"] == "low"
    assert second["thinking"] == deepseek.retry_body["thinking"]
    assert "reasoning_effort" not in second, "the fallback body replaces the entry's own"


def test_an_entry_without_a_fallback_reports_the_empty_reply(tmp_path: Path) -> None:
    """No declared fallback means no invented one: the failure is reported as it happened."""
    plain = provider("openai")
    assert not plain.retry_body
    transport = Recorder(openai_reply("", finish_reason="length"))

    result = backend_for(plain, transport).author(request_for(tmp_path))

    assert result.ok is False
    assert "response_empty" in result.detail, result.detail
    assert "length" in result.detail, "the stop reason is named"
    assert len(transport.requests) == 1, "nothing else is sent"


def test_a_malformed_reply_is_retried_once_with_the_parse_error(tmp_path: Path) -> None:
    """A slip in the reply costs one extra request, not the whole build.

    The retry is told what was wrong, and the second answer is what lands on disk; the
    harness still judges whatever is written, so nothing about the verdict changes.
    """
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT}})
    transport = Sequenced(
        (200, openai_reply("this is not JSON at all")), (200, openai_reply(reply))
    )
    request = request_for(tmp_path)

    result = backend_for(provider("deepseek"), transport).author(request)

    assert result.ok is True, result.detail
    assert (request.model_dir / f"{SUBCKT}.lib").read_text(encoding="utf-8") == LIB_TEXT
    assert len(transport.requests) == 2, "exactly one retry"
    reasked = json.loads(transport.requests[1].body)["messages"][0]["content"]
    assert "rejected" in reasked and "one JSON object" in reasked
    assert result.usage["prompt_tokens"] == 22.0, "both requests are counted"


def test_a_retry_that_also_fails_names_both_attempts(tmp_path: Path) -> None:
    """Two malformed replies are one honest failure, with both reasons in the detail."""
    transport = Sequenced(
        (200, openai_reply("not JSON")), (200, openai_reply("{still: not, json}"))
    )
    request = request_for(tmp_path)

    result = backend_for(provider("deepseek"), transport).author(request)

    assert result.ok is False
    assert "api_reply_unparsed" in result.detail
    assert "the retry was rejected too" in result.detail, result.detail
    assert written_files(tmp_path) == []


def test_a_file_already_named_by_the_prompt_lands_under_model(tmp_path: Path) -> None:
    """The prompt says ``model/<SUBCKT>.lib``; the backend writes under ``model/``."""
    reply = json.dumps({"files": {f"model/{SUBCKT}.lib": LIB_TEXT}})
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), Recorder(openai_reply(reply))).author(request)

    assert result.ok is True, result.detail
    assert (request.model_dir / f"{SUBCKT}.lib").is_file()


@pytest.mark.parametrize(
    "name",
    ["../escape.lib", "/absolute/evil.lib", "C:/windows/evil.lib", f"model/../../{SUBCKT}.lib"],
)
def test_a_path_outside_the_model_directory_writes_nothing(name: str, tmp_path: Path) -> None:
    reply = json.dumps({"files": {name: LIB_TEXT}})
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), Recorder(openai_reply(reply))).author(request)

    assert result.ok is False
    assert result.detail.startswith("api_write_refused:")
    assert name.split("/")[-1] in result.detail or name in result.detail
    assert written_files(tmp_path) == []


def test_one_bad_path_writes_none_of_the_files(tmp_path: Path) -> None:
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT, "../overflow.lib": ASY_TEXT}})
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), Recorder(openai_reply(reply))).author(request)

    assert result.ok is False and result.detail.startswith("api_write_refused:")
    assert written_files(tmp_path) == [], "a refused reply must not be half-applied"


# --------------------------------------------------------------------------- #
# the other two wires


def test_the_anthropic_wire_sends_and_reads_the_documented_shape(tmp_path: Path) -> None:
    schema_holder = provider("anthropic")
    reply = json.dumps(
        {
            "content": [{"type": "text", "text": LIB_TEXT}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 9},
        }
    )
    transport = Recorder(reply)
    request = request_for(tmp_path, expect_text=True)

    result = backend_for(schema_holder, transport).author(request)

    assert result.ok is True and result.stdout_tail == LIB_TEXT
    assert result.usage == {"input_tokens": 4.0, "output_tokens": 9.0}
    sent = transport.requests[0]
    assert sent.url == f"{schema_holder.endpoint}/messages"
    assert sent.headers["x-api-key"] == SECRET
    assert sent.headers["anthropic-version"] == api_backend.ANTHROPIC_VERSION
    body = json.loads(sent.body)
    assert body["model"] == schema_holder.model
    assert body["max_tokens"] == api_backend.MAX_OUTPUT_TOKENS
    assert body["messages"][0]["content"].startswith("author the model")


def test_the_google_wire_sends_and_reads_the_documented_shape(tmp_path: Path) -> None:
    schema_holder = provider("google")
    reply = json.dumps(
        {
            "candidates": [{"content": {"parts": [{"text": LIB_TEXT}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 12},
        }
    )
    transport = Recorder(reply)
    request = request_for(tmp_path)

    result = backend_for(schema_holder, transport).author(
        AuthorRequest(
            prompt=request.prompt,
            workdir=request.workdir,
            model_dir=request.model_dir,
            max_turns=1,
        )
    )

    assert result.ok is False, "a text reply is not the files shape"
    assert result.detail.startswith("api_reply_unparsed:")
    sent = transport.requests[0]
    assert sent.url == f"{schema_holder.endpoint}/models/{schema_holder.model}:generateContent"
    assert sent.headers["x-goog-api-key"] == SECRET
    body = json.loads(sent.body)
    assert body["contents"][0]["parts"][0]["text"].startswith("author the model")
    assert body["generationConfig"]["maxOutputTokens"] == api_backend.MAX_OUTPUT_TOKENS


def test_a_google_text_reply_is_read_from_the_candidate_parts(tmp_path: Path) -> None:
    reply = json.dumps({"candidates": [{"content": {"parts": [{"text": "the answer"}]}}]})
    request = request_for(tmp_path, expect_text=True)

    result = backend_for(provider("google"), Recorder(reply)).author(request)

    assert result.ok is True and result.stdout_tail == "the answer"


# --------------------------------------------------------------------------- #
# strict reply handling and operator-readable failures


def test_a_fenced_reply_is_not_unwrapped_and_writes_nothing(tmp_path: Path) -> None:
    fenced = "```json\n" + json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}}) + "\n```"
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), Recorder(openai_reply(fenced))).author(request)

    assert result.ok is False and result.detail.startswith("api_reply_unparsed:")
    assert written_files(tmp_path) == []


def test_a_reply_with_an_empty_files_map_is_invalid(tmp_path: Path) -> None:
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), Recorder(openai_reply('{"files": {}}'))).author(
        request
    )

    assert result.ok is False and result.detail.startswith("api_reply_invalid:")
    assert written_files(tmp_path) == []


def test_a_truncated_reply_is_reported_as_truncated_not_merely_malformed(
    tmp_path: Path,
) -> None:
    cut_off = '{"files": {"BM_REG_BUCK.lib": ".subckt BM_REG_BUCK VIN SW'
    request = request_for(tmp_path)

    result = backend_for(
        provider("openai"), Recorder(openai_reply(cut_off, finish_reason="length"))
    ).author(request)

    assert result.ok is False
    assert result.detail.startswith("api_reply_unparsed:")
    assert "finish_reason='length'" in result.detail
    assert written_files(tmp_path) == []


def test_an_empty_answer_names_the_stop_reason(tmp_path: Path) -> None:
    """A reasoning model can spend its whole budget before writing an answer."""
    reply = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "", "reasoning_content": "..."},
                    "finish_reason": "length",
                }
            ]
        }
    )
    request = request_for(tmp_path)

    result = backend_for(provider("deepseek"), Recorder(reply)).author(request)

    assert result.ok is False
    assert result.detail.startswith("api_request_failed: response_empty:")
    assert "output-token limit (finish_reason='length')" in result.detail
    assert "api_reply_unparsed" not in result.detail, "a truncation is not a malformed reply"
    assert written_files(tmp_path) == []


def test_a_configured_output_budget_reaches_the_request_body(tmp_path: Path) -> None:
    schema_holder = provider("deepseek")
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))

    result = backend_for(schema_holder, transport, max_output_tokens=1234).author(
        request_for(tmp_path)
    )

    assert result.ok is True, result.detail
    body = json.loads(transport.requests[0].body)
    assert body["max_tokens"] == 1234 and body["temperature"] == 0.0


def test_a_per_call_timeout_bounds_the_http_request(tmp_path: Path) -> None:
    """The search budget must reach the transport even when the backend is injected."""
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))

    result = backend_for(provider("deepseek"), transport, timeout_s=600.0).author(
        request_for(tmp_path), timeout_s=12.5
    )

    assert result.ok is True, result.detail
    assert 0 < transport.requests[0].timeout_s <= 12.5


def test_build_api_backend_resolves_the_budget_explicit_then_configured() -> None:
    configured = AppConfig(agent_max_tokens=4096)

    explicit = build_api_backend("openai", max_tokens=2048, config=configured)
    from_config = build_api_backend("openai", config=configured)
    from_default = build_api_backend("openai", config=AppConfig())

    assert isinstance(explicit, ApiKeyBackend) and explicit.max_output_tokens == 2048
    assert isinstance(from_config, ApiKeyBackend) and from_config.max_output_tokens == 4096
    assert isinstance(from_default, ApiKeyBackend) and from_default.max_output_tokens is None


def test_an_anthropic_and_google_turn_carry_the_budget_too(tmp_path: Path) -> None:
    anthropic_transport = Recorder(json.dumps({"content": [{"text": "hi"}]}))
    google_transport = Recorder(
        json.dumps({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    )

    backend_for(provider("anthropic"), anthropic_transport, max_output_tokens=777).author(
        request_for(tmp_path, expect_text=True)
    )
    backend_for(provider("google"), google_transport, max_output_tokens=777).author(
        request_for(tmp_path, expect_text=True)
    )

    assert json.loads(anthropic_transport.requests[0].body)["max_tokens"] == 777
    assert (
        json.loads(google_transport.requests[0].body)["generationConfig"]["maxOutputTokens"] == 777
    )


def test_reasoning_content_is_never_taken_as_the_reply(tmp_path: Path) -> None:
    reply = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}}),
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    request = request_for(tmp_path)

    result = backend_for(provider("deepseek"), Recorder(reply)).author(request)

    assert result.ok is False
    assert written_files(tmp_path) == []


def test_an_authentication_failure_is_retried_only_when_retryable(tmp_path: Path) -> None:
    transport = Sequenced((401, '{"error": "nope"}'), (200, openai_reply("{}")))
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), transport).author(request)

    assert result.ok is False
    assert result.detail.startswith("api_request_failed: http_auth_error:")
    assert len(transport.requests) == 1, "a 401 is not retried"


def test_a_retryable_status_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_backend, "_sleep", lambda seconds: None)
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}})
    transport = Sequenced((503, "busy"), (200, openai_reply(reply)))
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), transport, retries=1).author(request)

    assert result.ok is True, result.detail
    assert len(transport.requests) == 2
    assert (request.model_dir / f"{SUBCKT}.lib").is_file()


def test_a_cancelled_turn_is_reported_and_writes_nothing(tmp_path: Path) -> None:
    transport = Recorder(openai_reply("{}"))
    cancel = threading.Event()
    cancel.set()
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), transport).author(request, cancel)

    assert result.ok is False and result.detail.startswith("cancelled")
    assert transport.requests == []


def test_a_bad_response_body_is_reported_with_a_redacted_excerpt(tmp_path: Path) -> None:
    transport = Recorder(f"not json, key={SECRET}", status=200)
    request = request_for(tmp_path)

    result = backend_for(provider("openai"), transport).author(request)

    assert result.ok is False and result.detail.startswith("api_request_failed: response_not_json:")
    assert SECRET not in result.detail


# --------------------------------------------------------------------------- #
# secrets


def test_a_missing_key_names_every_source_and_no_value() -> None:
    backend = ApiKeyBackend(
        provider("deepseek"), transport=Recorder(""), credential_lookup=without_key()
    )

    usable, reason = backend.availability()

    assert usable is False
    assert reason.startswith("api_key_unavailable:")
    assert "deepseek" in reason
    assert "boardmodeler" in reason and "keyring" in reason
    assert "BOARDMODELER_DEEPSEEK_API_KEY" in reason and "DEEPSEEK_API_KEY" in reason
    assert SECRET not in reason


def test_a_vendor_alias_environment_variable_is_read_like_the_message_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every variable the reason advertises must actually work."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))
    backend = ApiKeyBackend(
        provider("deepseek"), transport=transport, credential_lookup=without_key()
    )

    usable, reason = backend.availability()

    assert usable is True, reason
    assert "DEEPSEEK_API_KEY" in reason, "the matched variable must be named"
    assert SECRET not in reason

    result = backend.author(request_for(tmp_path))

    assert result.ok is True, result.detail
    assert len(transport.requests) == 1, "an advertised alias must produce a real attempt"
    assert transport.requests[0].headers["Authorization"] == f"Bearer {SECRET}"


class EmptyKeyring:
    """A keyring with nothing in it, so a real one cannot answer for the test."""

    def get_password(self, service: str, key: str) -> None:
        return None


def test_the_boardmodeler_variable_is_read_before_the_vendor_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.security import credentials

    monkeypatch.setattr(credentials, "keyring", EmptyKeyring())
    monkeypatch.setenv("BOARDMODELER_DEEPSEEK_API_KEY", "boardmodeler-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "vendor-value")
    transport = Recorder(openai_reply("{}"))

    result = ApiKeyBackend(provider("deepseek"), transport=transport).author(request_for(tmp_path))

    assert result.ok is False, "the turn was attempted (an empty reply is not the files shape)"
    assert transport.requests[0].headers["Authorization"] == "Bearer boardmodeler-value"


def test_the_key_never_appears_when_a_server_echoes_it_back(tmp_path: Path) -> None:
    """Both a failed request and an unparseable reply are redacted."""
    request = request_for(tmp_path)
    failed = backend_for(
        provider("openai"), Recorder(f"the key {SECRET} was rejected", status=401)
    ).author(request)

    assert failed.ok is False
    assert SECRET not in failed.detail and SECRET not in failed.stdout_tail
    assert REDACTED in failed.detail

    echoed = backend_for(
        provider("openai"), Recorder(openai_reply(f"prose that echoes {SECRET}, no JSON"))
    ).author(request)

    assert echoed.ok is False and echoed.detail.startswith("api_reply_unparsed:")
    assert SECRET not in echoed.detail and SECRET not in echoed.stdout_tail
    assert REDACTED in echoed.stdout_tail


def test_availability_reports_an_unconfigured_endpoint_or_model() -> None:
    no_endpoint = api_backend.AgentProvider(
        id=OPENAI.id,
        label=OPENAI.label,
        wire=OPENAI.wire,
        credential=OPENAI.credential,
        key_label=OPENAI.key_label,
        key_hint=OPENAI.key_hint,
        docs=OPENAI.docs,
        endpoint=None,
        model=OPENAI.model,
    )
    no_model = api_backend.AgentProvider(
        id=DEEPSEEK.id,
        label=DEEPSEEK.label,
        wire=DEEPSEEK.wire,
        credential=DEEPSEEK.credential,
        key_label=DEEPSEEK.key_label,
        key_hint=DEEPSEEK.key_hint,
        docs=DEEPSEEK.docs,
        endpoint=DEEPSEEK.endpoint,
        model="",
    )

    assert ApiKeyBackend(no_endpoint, credential_lookup=with_key()).availability() == (
        False,
        f"api_endpoint_not_configured: provider {OPENAI.id!r} ships no endpoint in this build; "
        f"document one from {OPENAI.docs}",
    )
    usable, reason = ApiKeyBackend(no_model, credential_lookup=with_key()).availability()
    assert usable is False and reason.startswith("api_model_not_configured:")


def test_a_provider_outside_this_builds_catalog_is_refused() -> None:
    stranger = api_backend.AgentProvider(
        id="not-in-this-build",
        label="Stranger",
        wire="openai",
        credential="stranger",
        key_label="STRANGER API KEY",
        key_hint="",
        docs="https://example.invalid/docs",
        endpoint="https://example.invalid/v1",
        model="stranger-1",
    )

    usable, reason = ApiKeyBackend(stranger, credential_lookup=with_key()).availability()

    assert usable is False
    assert reason.startswith("api_provider_unavailable:")
    assert all(f"'{name}'" in reason for name in ids())


def test_availability_is_true_with_a_key_and_names_the_source() -> None:
    backend = ApiKeyBackend(provider("openai"), credential_lookup=with_key())

    usable, reason = backend.availability()

    assert usable is True
    assert "openai" in reason.lower() and "keyring" in reason
    assert SECRET not in reason


# --------------------------------------------------------------------------- #
# text mode


def test_a_text_turn_writes_no_file_and_returns_the_reply(tmp_path: Path) -> None:
    transport = Recorder(openai_reply('{"sources": [{"url": "https://x.invalid"}]}'))
    request = request_for(tmp_path, expect_text=True)

    result = backend_for(provider("openai"), transport).author(request)

    assert result.ok is True
    assert result.stdout_tail == '{"sources": [{"url": "https://x.invalid"}]}'
    assert written_files(tmp_path) == []
    prompt = json.loads(transport.requests[0].body)["messages"][0]["content"]
    assert "REPLY FORMAT" not in prompt, "a text turn must not be asked for files"


def test_a_text_turn_returns_prose_even_when_it_is_not_json(tmp_path: Path) -> None:
    request = request_for(tmp_path, expect_text=True)

    result = backend_for(
        provider("openai"), Recorder(openai_reply("No sources found for that part."))
    ).author(request)

    assert result.ok is True
    assert result.stdout_tail == "No sources found for that part."
    assert written_files(tmp_path) == []


# --------------------------------------------------------------------------- #
# the factory


def test_build_api_backend_returns_bob_shell_for_bob() -> None:
    backend = build_api_backend("bob", config=AppConfig())
    with_team = build_api_backend("bob", team_id="team-7", config=AppConfig())

    assert isinstance(backend, BobShellBackend)
    assert isinstance(with_team, BobShellBackend) and with_team.team_id == "team-7"


def test_build_api_backend_refuses_an_unknown_id_and_names_the_catalog() -> None:
    backend = build_api_backend("magic", config=AppConfig())

    assert isinstance(backend, UnavailableBackend)
    usable, reason = backend.availability()
    assert usable is False
    assert reason.startswith("api_provider_unavailable:")
    assert all(f"'{name}'" in reason for name in ids())


def test_build_api_backend_follows_the_explicit_then_configured_order() -> None:
    configured = AppConfig(agent_provider="deepseek", agent_model="deepseek-flash")

    explicit = build_api_backend("openai", config=configured)
    fallback = build_api_backend(None, config=configured)

    assert isinstance(explicit, ApiKeyBackend) and explicit.provider.id == "openai"
    assert isinstance(fallback, ApiKeyBackend)
    assert fallback.provider.id == "deepseek" and fallback.model == "deepseek-flash"


def test_build_api_backend_resolves_the_model_explicit_then_configured() -> None:
    configured = AppConfig(agent_model="configured-model")

    explicit = build_api_backend("openai", model="explicit-model", config=configured)
    from_config = build_api_backend("openai", config=configured)
    from_catalog = build_api_backend("openai", config=AppConfig())

    assert isinstance(explicit, ApiKeyBackend) and explicit.model == "explicit-model"
    assert isinstance(from_config, ApiKeyBackend) and from_config.model == "configured-model"
    assert isinstance(from_catalog, ApiKeyBackend) and from_catalog.model is None


def test_the_providers_documented_model_is_what_a_turn_sends(tmp_path: Path) -> None:
    schema_holder = provider("openai")
    reply = json.dumps({"files": {f"{SUBCKT}.lib": LIB_TEXT}})
    transport = Recorder(openai_reply(reply))

    result = backend_for(schema_holder, transport).author(request_for(tmp_path))

    assert result.ok is True, result.detail
    assert json.loads(transport.requests[0].body)["model"] == schema_holder.model


def test_build_api_backend_never_falls_back_from_a_configured_provider() -> None:
    configured = AppConfig(agent_provider="not-in-this-build")

    backend = build_api_backend(None, config=configured)

    assert isinstance(backend, UnavailableBackend)
    assert "not-in-this-build" in backend.availability()[1]


def test_credential_for_walks_the_catalog_sources_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from boardmodeler.security import credentials

    monkeypatch.setattr(credentials, "keyring", EmptyKeyring())
    monkeypatch.delenv("BOARDMODELER_DEEPSEEK_API_KEY", raising=False)
    entry = provider("deepseek")
    assert api_backend.env_sources(entry)[0] == "BOARDMODELER_DEEPSEEK_API_KEY"

    missing = api_backend.credential_for(entry)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-the-alias")
    alias = api_backend.credential_for(entry)
    monkeypatch.setenv("BOARDMODELER_DEEPSEEK_API_KEY", "from-the-repo-variable")
    repo_variable = api_backend.credential_for(entry)

    assert missing.value is None and missing.source is SecretSource.MISSING
    assert alias.value == "from-the-alias" and alias.source is SecretSource.ENV
    assert alias.detail == "environment variable DEEPSEEK_API_KEY"
    assert repo_variable.value == "from-the-repo-variable"


def test_the_wrong_wire_is_a_programming_error_not_a_silent_coercion() -> None:
    with pytest.raises(ValueError, match="bob-shell"):
        ApiKeyBackend(provider("bob"), credential_lookup=with_key())


def test_constructor_validates_its_bounds() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        ApiKeyBackend(provider("openai"), timeout_s=0.0)
    with pytest.raises(ValueError, match="retries"):
        ApiKeyBackend(provider("openai"), retries=-1)
    with pytest.raises(ValueError, match="max_output_tokens"):
        ApiKeyBackend(provider("openai"), max_output_tokens=0)


def test_the_availability_reason_is_the_author_result_detail_for_a_missing_key(
    tmp_path: Path,
) -> None:
    backend = ApiKeyBackend(
        provider("deepseek"), transport=Recorder(""), credential_lookup=without_key()
    )

    result: AuthorResult = backend.author(request_for(tmp_path))

    assert result.ok is False
    assert result.detail == backend.availability()[1]
    assert written_files(tmp_path) == []
