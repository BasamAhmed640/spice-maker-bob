"""Fixture provider and registry selection (D11).

These tests prove the properties the pipeline relies on: replay returns exactly
the authored payload with a stable content hash, a changed input is a different
cache key, a missing fixture is an explicit ``fixture_missing`` error, nothing
opens a socket, a set cancel event stops the call, and provider selection never
silently substitutes a different provider.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from boardmodeler.config import AppConfig, ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.providers.base import (
    MAX_SNIPPET_CHARS,
    DocSnippet,
    ExtractionRequest,
    ExtractionTask,
    ProviderError,
    request_hash,
)
from boardmodeler.providers.fixture import FixtureProvider
from boardmodeler.providers.registry import build_provider, select_provider
from boardmodeler.security import credentials
from boardmodeler.security.policy import DataPolicy

SNIPPET_TEXT = "the input range is 4.5 V to 60 V"


class NoKeyring:
    """Keyring backend with no entries, for hermetic credential checks."""

    def get_password(self, service: str, key: str) -> str | None:
        return None


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)


def make_request(
    *,
    task: ExtractionTask = ExtractionTask.REQUIREMENTS,
    prompt: str = "extract the limits",
    schema_json: str = '{"type": "object"}',
    snippets: tuple[tuple[str, int, str], ...] = (("doc-1", 1, SNIPPET_TEXT),),
) -> ExtractionRequest:
    return ExtractionRequest(
        task=task,
        prompt=prompt,
        schema_json=schema_json,
        snippets=tuple(
            DocSnippet(doc_id=doc_id, pdf_page=page, printed_label=None, text=text)
            for doc_id, page, text in snippets
        ),
    )


def test_authored_fixture_replays_the_exact_payload(tmp_path: Path) -> None:
    provider = FixtureProvider(tmp_path)
    request = make_request()
    payload = {"requirements": [{"id": "R1", "min": 4.5, "unit": "V"}]}

    path = provider.write_fixture(request, payload, notes="authored by test")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"key", "provider", "model", "task", "payload", "notes", "created_utc"}
    assert stored["provider"] == "fixture"
    assert stored["model"] is None
    assert stored["task"] == "REQUIREMENTS"

    response = provider.extract(request)
    assert response.payload == payload
    assert response.from_cache is False
    assert response.request_hash == provider.key_for(request) == stored["key"]
    assert response.detail == "authored by test"
    assert response.identity.kind is ProviderKind.FIXTURE
    assert response.identity.usage_units == "none"
    assert response.identity.provider == "fixture"


def test_replay_is_deterministic_and_does_no_outbound_work(
    tmp_path: Path, no_network: None
) -> None:
    provider = FixtureProvider(tmp_path)
    request = make_request()
    provider.write_fixture(request, {"ok": True})

    first = provider.extract(request)
    second = provider.extract(request)
    assert first.request_hash == second.request_hash
    assert first.payload == second.payload == {"ok": True}
    assert provider.requests == [first.request_hash, first.request_hash]


def test_missing_fixture_names_the_key_and_the_searched_path(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    provider = FixtureProvider(fixture_dir)
    request = make_request()
    key = provider.key_for(request)

    with pytest.raises(ProviderError) as info:
        provider.extract(request)
    assert info.value.code == "fixture_missing"
    assert key in info.value.detail
    assert str(fixture_dir / f"{key}.json") in info.value.detail
    assert provider.requests == [key]


def test_request_hash_changes_with_every_input(tmp_path: Path) -> None:
    provider = FixtureProvider(tmp_path)
    base = make_request()
    baseline = provider.key_for(base)

    variants = {
        "snippet text": make_request(snippets=(("doc-1", 1, SNIPPET_TEXT + "."),)),
        "snippet page": make_request(snippets=(("doc-1", 2, SNIPPET_TEXT),)),
        "snippet doc": make_request(snippets=(("doc-2", 1, SNIPPET_TEXT),)),
        "snippet order": make_request(
            snippets=(("doc-2", 1, "second"), ("doc-1", 1, SNIPPET_TEXT))
        ),
        "task": make_request(task=ExtractionTask.PINMAP),
        "prompt": make_request(prompt="a different prompt"),
        "schema": make_request(schema_json='{"type": ["object"]}'),
    }
    for label, variant in variants.items():
        assert provider.key_for(variant) != baseline, f"{label} must change the key"
    ordered = make_request(snippets=(("doc-1", 1, SNIPPET_TEXT), ("doc-2", 1, "second")))
    reversed_order = make_request(snippets=(("doc-2", 1, "second"), ("doc-1", 1, SNIPPET_TEXT)))
    assert provider.key_for(ordered) != provider.key_for(reversed_order)
    assert request_hash(base, provider="other", model=None) != baseline
    assert request_hash(base, provider="fixture", model="a-model") != baseline


_SUBPROCESS_PROGRAM = """
from boardmodeler.providers.base import DocSnippet, ExtractionRequest, ExtractionTask, request_hash

snippet = DocSnippet(
    doc_id="doc-1", pdf_page=1, printed_label=None, text="the input range is 4.5 V to 60 V"
)
request = ExtractionRequest(
    task=ExtractionTask.REQUIREMENTS,
    prompt="extract the limits",
    schema_json='{"type": "object"}',
    snippets=(snippet,),
)
print(request_hash(request, provider="fixture", model=None))
"""


def test_request_hash_is_stable_across_processes() -> None:
    expected = request_hash(make_request(), provider="fixture", model=None)
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_PROGRAM],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_a_set_cancel_event_stops_the_extraction(tmp_path: Path) -> None:
    provider = FixtureProvider(tmp_path)
    request = make_request()
    provider.write_fixture(request, {"ok": True})
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(ProviderError) as info:
        provider.extract(request, cancel=cancel)
    assert info.value.code == "cancelled"
    assert provider.requests == []


def test_doc_snippet_rejects_text_over_the_page_cap() -> None:
    with pytest.raises(ValueError, match=str(MAX_SNIPPET_CHARS)):
        DocSnippet(
            doc_id="doc-1",
            pdf_page=0,
            printed_label=None,
            text="x" * (MAX_SNIPPET_CHARS + 1),
        )
    accepted = DocSnippet(
        doc_id="doc-1", pdf_page=0, printed_label=None, text="x" * MAX_SNIPPET_CHARS
    )
    assert len(accepted.text) == MAX_SNIPPET_CHARS


def test_health_reports_a_missing_fixture_directory(tmp_path: Path) -> None:
    missing = FixtureProvider(tmp_path / "absent")
    assert missing.health(1.0).ok is False
    assert missing.health(1.0).code == "fixture_dir_missing"
    present = FixtureProvider(tmp_path)
    assert present.health(1.0).ok is True


def test_build_provider_uses_the_requested_fixture_directory(tmp_path: Path) -> None:
    provider = build_provider(AppConfig(), name="fixture", fixture_dir=tmp_path)
    assert isinstance(provider, FixtureProvider)
    assert provider.fixture_dir == tmp_path


def test_build_provider_rejects_unknown_names() -> None:
    with pytest.raises(ProviderError) as info:
        build_provider(AppConfig(), name="not-configured")
    assert info.value.code == "provider_not_configured"


def test_select_provider_defaults_to_fixture_when_ordered_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.setattr(credentials, "_read_saved", lambda: None)
    selection = select_provider(
        AppConfig(), requested=None, allow_bob_shell=False, fixture_dir=tmp_path
    )
    assert selection.kind is ProviderKind.FIXTURE
    assert isinstance(selection.provider, FixtureProvider)
    assert "FIXTURE" in selection.detail


def test_select_provider_bob_direct_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOARDMODELER_BOB_DIRECT_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "_read_saved", lambda: None)

    with pytest.raises(ProviderError) as info:
        select_provider(AppConfig(), requested=ProviderKind.BOB_DIRECT, allow_bob_shell=False)
    assert info.value.code == "bob_direct_unsupported"


def test_select_provider_bob_shell_requires_policy_and_switch() -> None:
    with pytest.raises(ProviderError) as info:
        select_provider(
            AppConfig(data_policy=DataPolicy(allow_bob_shell=True)),
            requested=ProviderKind.BOB_SHELL,
            allow_bob_shell=False,
        )
    assert info.value.code == "legacy_bob_shell_disabled"

    with pytest.raises(ProviderError) as info:
        select_provider(AppConfig(), requested=ProviderKind.BOB_SHELL, allow_bob_shell=True)
    assert info.value.code == "legacy_bob_shell_disabled"


def test_select_provider_rejects_unknown_kind_strings() -> None:
    with pytest.raises(ProviderError) as info:
        select_provider(AppConfig(), requested="telepathy", allow_bob_shell=False)
    assert info.value.code == "unknown_provider"


def test_walk_reports_every_rejection_when_nothing_is_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOARDMODELER_BOB_DIRECT_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "_read_saved", lambda: None)
    config = AppConfig(provider_order=[ProviderKind.BOB_DIRECT, ProviderKind.HTTP_INFERENCE])

    with pytest.raises(ProviderError) as info:
        select_provider(config, requested=None, allow_bob_shell=False)
    assert info.value.code == "no_provider_available"
    assert "BOB_DIRECT: bob_direct_unsupported" in info.value.detail
    assert "HTTP_INFERENCE: http_inference_unsupported" in info.value.detail


def test_http_provider_requires_an_endpoint_and_model() -> None:
    config = AppConfig(
        providers={
            "http_inference": ProviderConfig(
                kind=ProviderKind.HTTP_INFERENCE,
                endpoint="https://inference.example.invalid/v1",
            )
        }
    )
    with pytest.raises(ProviderError) as info:
        select_provider(config, requested=ProviderKind.HTTP_INFERENCE, allow_bob_shell=False)
    assert info.value.code == "http_inference_unsupported"
