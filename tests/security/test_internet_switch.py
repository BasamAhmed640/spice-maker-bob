"""Bob and supporting-material egress obey the one SETUP Internet switch."""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler.config import AppConfig, load_config, save_config
from boardmodeler.security.network import NetworkRefused, internet_allowed


def test_old_web_choice_migrates_to_the_one_switch(tmp_path: Path) -> None:
    config = AppConfig.model_validate({"web_reinforcement": False})
    assert config.internet_access is False
    path = tmp_path / "config.json"
    save_config(config, path)
    assert load_config(path).internet_access is False
    text = path.read_text(encoding="utf-8")
    assert "internet_access" in text and "web_reinforcement" not in text

    explicit = AppConfig.model_validate({"web_reinforcement": False, "internet_access": True})
    assert explicit.internet_access is True, "the new switch wins during migration"


def test_environment_can_force_the_switch_off(monkeypatch) -> None:
    monkeypatch.setattr("boardmodeler.config.load_config", lambda: AppConfig(internet_access=True))
    monkeypatch.delenv("BOARDMODELER_NO_NETWORK", raising=False)
    assert internet_allowed()
    monkeypatch.setenv("BOARDMODELER_NO_NETWORK", "1")
    assert not internet_allowed()


def test_bob_process_is_refused_before_spawn_when_off(tmp_path: Path, monkeypatch) -> None:
    from boardmodeler.authoring import backends

    monkeypatch.setenv("BOARDMODELER_NO_NETWORK", "1")
    monkeypatch.setattr(
        backends.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Bob must not start with Internet off"),
    )
    with pytest.raises(NetworkRefused, match="internet_access_off"):
        backends.run_bob_shell(["bob", "run"], cwd=tmp_path, timeout_s=1, env={})


def test_bob_author_and_key_check_do_not_call_a_runner_when_off(
    tmp_path: Path, monkeypatch
) -> None:
    from boardmodeler.agent_providers import default_provider
    from boardmodeler.authoring.backends import AuthorRequest, BobShellBackend
    from boardmodeler.security.key_verification import verify_key

    monkeypatch.setenv("BOARDMODELER_NO_NETWORK", "1")
    backend = BobShellBackend(runner=lambda *a, **k: pytest.fail("Bob runner was called"))
    assert backend.availability()[0] is False
    result = backend.author(AuthorRequest("prompt", tmp_path, tmp_path, 1))
    assert not result.ok and "internet_access_off" in result.detail

    checked = verify_key(default_provider(), "fixture-key")
    assert checked.status == "unverified" and "nothing was sent" in checked.detail
