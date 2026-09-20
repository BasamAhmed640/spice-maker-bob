"""Regressions for desktop consent retries and the real Go API contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardmodeler.agent_providers import by_id
from boardmodeler.authoring.api_backend import ApiKeyBackend
from boardmodeler.authoring.backends import AuthorRequest
from boardmodeler.documents.store import DocumentStore, DocumentStoreError
from boardmodeler.pipeline.make_model import MakeModelRequest, _Run, _StageLog
from boardmodeler.providers.http_inference import HttpResponse
from boardmodeler.security.credentials import Credential, SecretSource
from tests.documents.test_store import _write_datasheet


@pytest.mark.parametrize(
    ("provider_id", "path", "value"),
    [
        ("deepseek", ("reasoning_effort",), "max"),
        ("openai", ("reasoning_effort",), "max"),
        ("anthropic", ("output_config", "effort"), "max"),
        ("google", ("generationConfig", "thinkingConfig", "thinkingLevel"), "high"),
        ("openrouter", ("reasoning", "effort"), "max"),
        ("xai", ("reasoning_effort",), "xhigh"),
        ("opencode", ("reasoning_effort",), "max"),
        ("opencode_go", ("reasoning_effort",), "max"),
    ],
)
def test_supported_default_models_use_highest_reasoning(provider_id, path, value):
    provider = by_id(provider_id)
    if provider is None:
        pytest.skip("Provider not shipped in this edition")
    backend = ApiKeyBackend(provider)
    _, _, body = backend._shape("extract requirements or author model", key="test-secret")
    for field in path:
        body = body[field]
    assert body == value
    assert not provider.retry_body


def test_desktop_retry_can_grant_and_revoke_remote_permission(tmp_path: Path):
    pdf = tmp_path / "datasheet.pdf"
    _write_datasheet(pdf)
    records = []
    for consent in (False, True, False):
        run = _Run(
            MakeModelRequest(
                part="LM358",
                subckt="LM358",
                datasheet=pdf,
                out_dir=tmp_path / "model",
                allow_remote=consent,
            ),
            _StageLog(None),
        )
        run.read()
        assert run.record.remote_inference_allowed is consent
        assert run.store.get(run.record.doc_id).remote_inference_allowed is consent
        records.append(run.record)
    assert len({r.doc_id for r in records}) == 1
    assert len({r.file_hash for r in records}) == 1
    assert len({r.path for r in records}) == 1


def test_permission_update_does_not_allow_other_metadata_changes(tmp_path: Path):
    pdf = tmp_path / "datasheet.pdf"
    _write_datasheet(pdf)
    store = DocumentStore(tmp_path / "project")
    original = store.add_file(pdf, doc_type="datasheet", provenance="user_supplied")
    with pytest.raises(DocumentStoreError, match="different metadata"):
        store.add_file(
            pdf,
            doc_type="datasheet",
            provenance="user_supplied",
            remote_inference_allowed=True,
        )
    with pytest.raises(DocumentStoreError, match="different metadata"):
        store.add_file(
            pdf,
            doc_type="datasheet",
            provenance="user_supplied",
            classification="public",
            update_remote_permission=True,
        )
    assert store.get(original.doc_id) == original


@pytest.mark.skipif(by_id("opencode_go") is None, reason="Bob-only catalog")
def test_go_uses_subscription_endpoint_max_reasoning_and_stable_session(tmp_path: Path):
    sent = []
    replies = ["not json", json.dumps({"files": {"divider.lib": "* synthetic fixture\n.end\n"}})]

    def transport(request):
        sent.append(request)
        return HttpResponse(
            200, {}, json.dumps({"choices": [{"message": {"content": replies.pop(0)}}]}).encode()
        )

    backend = ApiKeyBackend(
        by_id("opencode_go"),
        transport=transport,
        credential_lookup=lambda name: Credential(name, "test-secret", SecretSource.KEYRING, ""),
    )
    result = backend.author(AuthorRequest("write a SPICE netlist", tmp_path, tmp_path, 1))
    assert result.ok, result.detail
    assert len(sent) == 2
    for request in sent:
        assert request.url == "https://opencode.ai/zen/go/v1/chat/completions"
        body = json.loads(request.body)
        assert body["reasoning_effort"] == "max"
        assert body["thinking"] == {"type": "enabled"}
        assert request.headers["User-Agent"].startswith("SpiceMaker/")
    assert sent[0].headers["x-opencode-session"] == sent[1].headers["x-opencode-session"]
    assert not backend.provider.retry_body
    assert by_id("opencode").endpoint == "https://opencode.ai/zen/v1"


@pytest.mark.skipif(by_id("opencode_go") is None, reason="Bob-only catalog")
def test_go_credit_error_never_falls_back_to_zen(tmp_path: Path):
    sent = []

    def transport(request):
        sent.append(request)
        return HttpResponse(401, {}, b'{"error":{"message":"Insufficient balance"}}')

    backend = ApiKeyBackend(
        by_id("opencode_go"),
        transport=transport,
        credential_lookup=lambda name: Credential(name, "test-secret", SecretSource.KEYRING, ""),
    )
    result = backend.author(
        AuthorRequest("extract PDF requirements", tmp_path, tmp_path, 1, expect_text=True)
    )
    assert not result.ok
    assert "Insufficient balance" in result.detail
    assert len(sent) == 1
    assert "/zen/go/v1/" in sent[0].url
