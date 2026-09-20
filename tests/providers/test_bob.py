"""Bob providers (D11, A8).

Bob Direct must be blocked, never substituted, when it has no credential or no
documented endpoint; Bob Shell must stay off unless two switches are set, must
refuse documents that may not leave the machine, and must run one enumerated tool
set in one explicit working directory while reporting usage in turns.

Nothing here reaches the network: Bob Direct is exercised with an injected
transport, and Bob Shell with an injected process runner plus one real
``sys.executable`` child that prints a JSON payload.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from boardmodeler.config import AppConfig, ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.domain.records import DocumentRecord
from boardmodeler.providers.base import (
    DocSnippet,
    ExtractionRequest,
    ExtractionTask,
    ProviderError,
)
from boardmodeler.providers.bob import BOB_API_KEY_ENV, BobDirectProvider, BobShellProvider
from boardmodeler.providers.http_inference import HttpRequest, HttpResponse
from boardmodeler.providers.registry import build_provider, select_provider
from boardmodeler.security import credentials
from boardmodeler.security.subprocess_guard import GuardedProcess

SECRET = "bob-unit-test-secret"
ENDPOINT = "https://bob.example.invalid/v1/chat/completions"
MODEL = "bob-documented-model"


class EmptyKeyring:
    """A keyring with no entries, so only the environment can supply a credential."""

    def get_password(self, service: str, key: str) -> str | None:
        return None


@pytest.fixture
def no_bob_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_read_saved", lambda: None)
    for name in (
        BOB_API_KEY_ENV,
        "BOARDMODELER_BOB_DIRECT_API_KEY",
        "BOARDMODELER_BOB_SHELL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def bob_credential(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(credentials, "_read_saved", lambda: None)
    monkeypatch.setenv(BOB_API_KEY_ENV, SECRET)
    return SECRET


def make_request(
    *,
    documents: Sequence[DocumentRecord] = (),
    prompt: str = "extract the limits",
) -> ExtractionRequest:
    return ExtractionRequest(
        task=ExtractionTask.REQUIREMENTS,
        prompt=prompt,
        schema_json='{"type": "object"}',
        snippets=(DocSnippet(doc_id="DOC_1", pdf_page=0, printed_label=None, text="text"),),
        documents=tuple(documents),
    )


def document(classification: str, doc_id: str = "DOC_1") -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        title="Contract",
        doc_type="datasheet",
        file_hash="a" * 64,
        provenance="user_supplied",
        classification=classification,
        text_extraction="embedded",
    )


def chat_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"total_tokens": 42},
        }
    ).encode("utf-8")


def direct_provider(
    *,
    endpoint: str | None = ENDPOINT,
    model: str | None = MODEL,
    transport: Callable[[HttpRequest], HttpResponse] | None = None,
) -> BobDirectProvider:
    return BobDirectProvider(
        provider_config=ProviderConfig(
            kind=ProviderKind.BOB_DIRECT, endpoint=endpoint, model=model, timeout_s=5.0
        ),
        name="bob_direct",
        transport=transport,
        sleep=lambda _seconds: None,
    )


# --------------------------------------------------------------------------- #
# Bob Direct


def test_direct_without_credentials_is_blocked_and_sends_nothing(
    no_bob_credentials: None,
) -> None:
    calls: list[HttpRequest] = []

    def transport(request: HttpRequest) -> HttpResponse:
        calls.append(request)
        raise AssertionError("no request may be sent without a credential")

    provider = direct_provider(transport=transport)
    health = provider.health(1.0)
    assert health.ok is False
    assert health.code == "bob_credentials_unavailable"
    assert "nothing was sent" in health.detail

    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "bob_credentials_unavailable"
    assert calls == []


def test_direct_without_a_documented_endpoint_is_blocked(
    bob_credential: str,
) -> None:
    provider = direct_provider(endpoint=None, model=None)
    health = provider.health(1.0)
    assert health.ok is False
    assert health.code == "bob_endpoint_not_configured"
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "bob_endpoint_not_configured"


def test_bob_is_never_substituted_when_it_is_unusable(
    no_bob_credentials: None, tmp_path: Path
) -> None:
    config = AppConfig()
    config.providers["bob_direct"] = ProviderConfig(
        kind=ProviderKind.BOB_DIRECT, endpoint=ENDPOINT, model=MODEL
    )

    # A walk over a provider_order that only holds Bob fails with the reason.
    config.provider_order = [ProviderKind.BOB_DIRECT]
    with pytest.raises(ProviderError) as walk_error:
        select_provider(config, requested=None, allow_bob_shell=False)
    assert walk_error.value.code == "no_provider_available"
    assert "bob_credentials_unavailable" in walk_error.value.detail

    # With the fixture provider behind it, the walk selects the fixture — and an
    # explicit Bob request still fails instead of being redirected to it.
    config.provider_order = [ProviderKind.BOB_DIRECT, ProviderKind.FIXTURE]
    selection = select_provider(config, requested=None, allow_bob_shell=False)
    assert selection.kind is ProviderKind.FIXTURE
    with pytest.raises(ProviderError) as explicit_error:
        select_provider(config, requested=ProviderKind.BOB_DIRECT, allow_bob_shell=False)
    assert explicit_error.value.code == "bob_credentials_unavailable"


def test_direct_with_configuration_sends_an_authenticated_request(
    bob_credential: str,
) -> None:
    calls: list[HttpRequest] = []

    def transport(request: HttpRequest) -> HttpResponse:
        calls.append(request)
        return HttpResponse(status=200, headers={}, body=chat_body({"ok": True}))

    provider = direct_provider(transport=transport)
    response = provider.extract(make_request())

    assert len(calls) == 1
    sent = calls[0]
    assert sent.method == "POST"
    assert sent.url == ENDPOINT
    assert sent.headers["Authorization"] == f"Bearer {SECRET}"
    body = json.loads(sent.body.decode("utf-8"))
    assert body["model"] == MODEL
    assert response.payload == {"ok": True}
    identity = response.identity
    assert identity.kind is ProviderKind.BOB_DIRECT
    assert identity.usage_units == "tokens", "Bob Direct reports tokens"
    assert identity.usage == {"total_tokens": 42.0}
    assert identity.endpoint == ENDPOINT


# --------------------------------------------------------------------------- #
# Bob Shell


def shell_provider(
    workdir: Path,
    *,
    allow_bob_shell: bool = True,
    argv_template: Sequence[str] | None = None,
    tools: Sequence[str] = ("read_file", "write_file"),
    runner: Callable[..., GuardedProcess] | None = None,
) -> BobShellProvider:
    return BobShellProvider(
        provider_config=ProviderConfig(kind=ProviderKind.BOB_SHELL, timeout_s=5.0),
        name="bob_shell",
        allow_bob_shell=allow_bob_shell,
        argv_template=list(argv_template or [sys.executable, "-c", "print('{}')"]),
        tools=list(tools),
        workdir=workdir,
        runner=runner,
    )


def fake_runner(**result: Any) -> Callable[..., GuardedProcess]:
    calls: list[dict[str, Any]] = []

    def runner(argv: Sequence[str], **kwargs: Any) -> GuardedProcess:
        calls.append({"argv": list(argv), **kwargs})
        return GuardedProcess(
            returncode=int(result.get("returncode", 0)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            wall_s=float(result.get("wall_s", 0.01)),
            timed_out=bool(result.get("timed_out", False)),
        )

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_shell_is_disabled_unless_explicitly_allowed(tmp_path: Path) -> None:
    runner = fake_runner(stdout=json.dumps({"ok": True}))
    provider = shell_provider(tmp_path, allow_bob_shell=False, runner=runner)

    health = provider.health(1.0)
    assert health.ok is False
    assert health.code == "bob_shell_not_allowed"
    with pytest.raises(ProviderError) as excinfo:
        provider.extract(make_request())
    assert excinfo.value.code == "bob_shell_not_allowed"
    assert runner.calls == []  # type: ignore[attr-defined]

    config = AppConfig()
    with pytest.raises(ProviderError) as registry_error:
        build_provider(config, name="bob_shell")
    assert registry_error.value.code == "bob_shell_not_allowed"
    with pytest.raises(ProviderError) as selection_error:
        select_provider(config, requested=ProviderKind.BOB_SHELL, allow_bob_shell=False)
    assert selection_error.value.code == "bob_shell_not_allowed"


def test_shell_needs_a_command_template_tools_and_a_workdir(tmp_path: Path) -> None:
    no_template = BobShellProvider(
        provider_config=ProviderConfig(kind=ProviderKind.BOB_SHELL),
        name="bob_shell",
        allow_bob_shell=True,
        tools=["read_file"],
        workdir=tmp_path,
    )
    assert no_template.health(1.0).code == "bob_shell_argv_not_configured"

    no_tools = shell_provider(tmp_path, tools=())
    assert no_tools.health(1.0).code == "bob_shell_tools_not_enumerated"

    missing_workdir = shell_provider(tmp_path / "nope")
    assert missing_workdir.health(1.0).code == "bob_shell_workdir_missing"


def test_shell_refuses_documents_that_may_not_leave_the_machine(
    tmp_path: Path, bob_credential: str
) -> None:
    runner = fake_runner(stdout=json.dumps({"ok": True}))
    provider = shell_provider(tmp_path, runner=runner)
    request = make_request(documents=[document("internal"), document("public", doc_id="DOC_2")])

    with pytest.raises(ProviderError) as excinfo:
        provider.extract(request)
    assert excinfo.value.code == "bob_shell_classification_refused"
    assert "DOC_1" in excinfo.value.detail
    assert runner.calls == []  # type: ignore[attr-defined]


def test_shell_runs_the_enumerated_tools_in_the_explicit_workdir(
    tmp_path: Path, bob_credential: str
) -> None:
    script = tmp_path / "fake_bob.py"
    script.write_text(
        "import json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n",
        encoding="utf-8",
    )
    provider = shell_provider(
        tmp_path,
        argv_template=[
            sys.executable,
            str(script),
            "--tools",
            "{tools}",
            "--workdir",
            "{workdir}",
        ],
        runner=None,
    )
    response = provider.extract(make_request())

    assert response.payload["cwd"] == str(tmp_path)
    assert response.payload["argv"] == [
        "--tools",
        "read_file,write_file",
        "--workdir",
        str(tmp_path),
    ]
    identity = response.identity
    assert identity.kind is ProviderKind.BOB_SHELL
    assert identity.usage_units == "turns", "Bob Shell usage is turns, never tokens"
    assert identity.usage == {"turns": 1.0}
    assert identity.detail["tools"] == "read_file,write_file"
    assert identity.detail["workdir"] == str(tmp_path)
    assert identity.detail["exit_code"] == "0"


def test_shell_prompt_reaches_the_process_on_stdin(tmp_path: Path, bob_credential: str) -> None:
    runner = fake_runner(stdout=json.dumps({"ok": True}))
    provider = shell_provider(tmp_path, runner=runner)
    provider.extract(make_request(prompt="EXTRACT-THIS"))
    call = runner.calls[0]  # type: ignore[attr-defined]
    assert "EXTRACT-THIS" in str(call["input_text"])
    assert str(call["cwd"]) == str(tmp_path)


def test_shell_failures_and_timeouts_are_reported(tmp_path: Path, bob_credential: str) -> None:
    failed = fake_runner(returncode=2, stderr="boom")
    with pytest.raises(ProviderError) as failed_error:
        shell_provider(tmp_path, runner=failed).extract(make_request())
    assert failed_error.value.code == "bob_shell_failed"
    assert "2" in failed_error.value.detail

    timed_out = fake_runner(timed_out=True)
    with pytest.raises(ProviderError) as timeout_error:
        shell_provider(tmp_path, runner=timed_out).extract(make_request())
    assert timeout_error.value.code == "bob_shell_timeout"


def test_shell_health_reports_the_observed_configuration(
    tmp_path: Path, bob_credential: str
) -> None:
    health = shell_provider(tmp_path).health(1.0)
    assert health.ok is True
    assert health.code == "ok"
    assert "2 tool(s)" in health.detail
    assert str(tmp_path) in health.detail


def test_shell_cancellation_before_the_run(tmp_path: Path, bob_credential: str) -> None:
    runner = fake_runner(stdout=json.dumps({"ok": True}))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProviderError) as excinfo:
        shell_provider(tmp_path, runner=runner).extract(make_request(), cancel)
    assert excinfo.value.code == "cancelled"
    assert runner.calls == []  # type: ignore[attr-defined]
