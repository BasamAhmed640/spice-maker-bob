"""The text wizard keeps credential handling explicit and failure messages safe."""

from __future__ import annotations

import argparse
import io
import json

import pytest

from boardmodeler import agent_providers, setup_wizard
from boardmodeler.config import AppConfig
from boardmodeler.security.credentials import Credential, SecretSource
from boardmodeler.security.key_verification import KeyVerification


def test_ambient_environment_key_does_not_finish_setup(monkeypatch) -> None:
    provider = agent_providers.CATALOG[0]
    monkeypatch.setattr(
        setup_wizard,
        "get_credential",
        lambda name: Credential(name, "ambient-secret", SecretSource.ENV, "environment"),
    )
    monkeypatch.setattr(setup_wizard.sys, "stdin", io.StringIO(""))

    with pytest.raises(setup_wizard.SetupError, match="was not entered"):
        setup_wizard._key(provider, None, yes=True)


def test_piped_key_is_accepted_with_yes(monkeypatch) -> None:
    provider = agent_providers.CATALOG[0]
    monkeypatch.setattr(
        setup_wizard,
        "get_credential",
        lambda name: Credential(name, None, SecretSource.MISSING, "missing"),
    )
    monkeypatch.setattr(setup_wizard.sys, "stdin", io.StringIO("SENTINEL\n"))

    assert setup_wizard._key(provider, None, yes=True) == "SENTINEL"


def test_rejected_key_is_retained_without_printing_it(monkeypatch, tmp_path, capsys) -> None:
    provider = agent_providers.CATALOG[0]
    key = "SENTINEL_NEVER_OUTPUT"
    credential_file = tmp_path / "credentials.json"
    config = AppConfig()
    order: list[str] = []
    monkeypatch.setattr(setup_wizard, "load_config", lambda: config)
    monkeypatch.setattr(setup_wizard, "_check_ltspice", lambda value: value)
    monkeypatch.setattr(setup_wizard, "_model_folder", lambda value: "models")
    monkeypatch.setattr(setup_wizard, "_provider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(setup_wizard, "_internet", lambda *args, **kwargs: True)
    monkeypatch.setattr(setup_wizard, "_key", lambda *args, **kwargs: key)
    monkeypatch.setattr(
        "boardmodeler.security.credentials.credential_path", lambda: credential_file
    )
    monkeypatch.setattr("boardmodeler.security.network._env_pins_off", lambda: False)

    def save(_config: AppConfig) -> None:
        order.append("config")

    def verify(*args, **kwargs) -> KeyVerification:
        order.append("check")
        assert credential_file.is_file()
        return KeyVerification("rejected", "Provider rejected the key.")

    monkeypatch.setattr(setup_wizard, "save_config", save)
    monkeypatch.setattr("boardmodeler.security.key_verification.verify_key", verify)
    args = argparse.Namespace(
        ltspice="C:/LTspice.exe",
        model_dir="models",
        provider=provider.id,
        internet="on",
        key_env="TEST_KEY",
        yes=True,
    )

    with pytest.raises(setup_wizard.SetupError, match="Key check rejected") as error:
        setup_wizard._save(args)

    assert config.setup_complete is True
    assert order == ["config", "check"]
    assert json.loads(credential_file.read_text(encoding="utf-8"))["key"] == key
    assert key not in str(error.value) + capsys.readouterr().out
