"""The packaged author factory can construct only Bob."""

from __future__ import annotations

import pytest

from boardmodeler import agent_providers
from boardmodeler.authoring.api_backend import build_api_backend, credential_for, env_sources
from boardmodeler.authoring.backends import BobShellBackend, UnavailableBackend
from boardmodeler.config import AppConfig
from boardmodeler.security.credentials import Credential, SecretSource


def test_catalog_contains_only_bob_in_source():
    assert agent_providers.ids() == ("bob",)
    assert agent_providers.WIRES == ("bob-shell",)
    assert len(agent_providers.CATALOG) == 1
    assert agent_providers.only_provider().id == "bob"
    assert not agent_providers.only_provider().model_editable


@pytest.mark.parametrize("selected", [None, "bob", "BOB", " bob "])
def test_factory_builds_bob(selected):
    backend = build_api_backend(selected, config=AppConfig(), team_id="team-test", timeout_s=42)
    assert isinstance(backend, BobShellBackend)
    assert backend.team_id == "team-test"
    assert backend.timeout_s == 42


@pytest.mark.parametrize("selected", ["unsupported-agent", "obsolete-setting"])
def test_foreign_configuration_is_refused_without_echoing_it(selected):
    backend = build_api_backend(config=AppConfig(agent_provider=selected))
    assert isinstance(backend, UnavailableBackend)
    usable, reason = backend.availability()
    assert not usable
    assert "IBM Bob only" in reason and selected not in reason


def test_explicit_bob_selection_replaces_incompatible_config():
    assert isinstance(
        build_api_backend("bob", config=AppConfig(agent_provider="unsupported-agent")),
        BobShellBackend,
    )


def test_keyring_takes_precedence_over_bob_environment(monkeypatch):
    monkeypatch.setenv("BOB_API_KEY", "test-environment-secret")
    credential = credential_for(
        agent_providers.default_provider(),
        lambda name: Credential(name, "test-keyring-secret", SecretSource.LOCAL_FILE, ""),
    )
    assert credential.value == "test-keyring-secret"
    assert credential.source is SecretSource.LOCAL_FILE


def test_bob_environment_alias(monkeypatch):
    monkeypatch.setenv("BOB_API_KEY", "test-environment-secret")
    credential = credential_for(
        agent_providers.default_provider(),
        lambda name: Credential(name, None, SecretSource.MISSING, ""),
    )
    assert credential.value == "test-environment-secret"
    assert credential.source is SecretSource.ENV
    assert env_sources(agent_providers.default_provider()) == (
        "BOARDMODELER_BOB_SHELL_API_KEY",
        "BOB_API_KEY",
    )
