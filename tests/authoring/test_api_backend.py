"""The packaged author factory can construct only Bob."""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler import agent_providers
from boardmodeler.authoring import backends
from boardmodeler.authoring.api_backend import build_api_backend, credential_for, env_sources
from boardmodeler.authoring.backends import BobShellBackend, UnavailableBackend
from boardmodeler.config import AppConfig
from boardmodeler.security.credentials import Credential, SecretSource


def test_catalog_contains_only_bob_in_source():
    assert agent_providers.ids() == ("bob",)
    assert agent_providers.WIRES == ("bob-shell",)
    assert len(agent_providers.CATALOG) == 1
    only = agent_providers.only_provider()
    assert only is not None, "a one-entry catalog offers exactly one provider"
    assert only.id == "bob"
    assert not only.model_editable


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


# --------------------------------------------------------------------------- #
# the turn budget: one attempt, the caller's own deadline, no retry arithmetic
#
# The main edition's recorded production failure was a retry funded from the remainder of
# a turn deadline: the first attempt spent the budget, the retry started with seconds
# left, and the resulting clock failure was reported as the model's verdict. Bob's path is
# structurally different — ``BobShellBackend.author`` makes exactly one runner call and
# passes its deadline through unchanged — so these pin that shape rather than a
# ``MIN_ATTEMPT_S`` guard, which this backend has no retry to gate.


BOB_SENTINEL = "bob-budget-sentinel-4c1b"
BOB_EXE = r"C:\tools\bob\bob.exe"


def _recording_runner(monkeypatch, results):
    """Bob \"installed\" and keyed, with a runner that records and replays ``results``."""
    monkeypatch.setattr(backends.shutil, "which", lambda name: BOB_EXE if name == "bob" else None)
    monkeypatch.setattr(
        backends,
        "get_credential",
        lambda name, **kwargs: Credential(name, BOB_SENTINEL, SecretSource.ENV, "test key"),
    )
    calls: list[dict[str, object]] = []

    def runner(argv, *, cwd, timeout_s, env, cancel=None, input_text=None):
        calls.append({"argv": list(argv), "timeout_s": timeout_s, "input_text": input_text})
        return results.pop(0)

    return calls, runner


def _failed_run(*, timed_out: bool = False):
    from boardmodeler.security.subprocess_guard import GuardedProcess

    return GuardedProcess(
        returncode=-1 if timed_out else 1,
        stdout="no JSON here",
        stderr="boom",
        wall_s=0.01,
        timed_out=timed_out,
    )


def test_the_factory_passes_the_turn_deadline_through_verbatim():
    backend = build_api_backend("bob", config=AppConfig(), timeout_s=42.0)
    assert isinstance(backend, BobShellBackend)
    assert backend.timeout_s == 42.0


def test_one_turn_is_one_runner_call_that_keeps_the_callers_deadline(monkeypatch, tmp_path: Path):
    """No remainder arithmetic and no retry: a failed turn ends the turn."""
    from boardmodeler.authoring.backends import AuthorRequest

    calls, runner = _recording_runner(monkeypatch, [_failed_run()])
    backend = build_api_backend("bob", config=AppConfig(), timeout_s=42.0)
    assert isinstance(backend, BobShellBackend)
    backend.runner = runner

    result = backend.author(
        AuthorRequest(prompt="author a model", workdir=tmp_path, model_dir=tmp_path, max_turns=1)
    )

    assert not result.ok
    assert len(calls) == 1, "a failed turn must not be retried out of a remainder"
    assert calls[0]["timeout_s"] == 42.0, "the caller's whole deadline reaches the runner"
    assert "bob_shell_output_unparsed" in result.detail
    assert "timeout" not in result.detail, (
        "the observed reason is kept, not replaced by a clock one"
    )


def test_a_small_explicit_deadline_is_honoured_not_rewritten(monkeypatch, tmp_path: Path):
    """The caller's own bound is theirs: nothing here inflates it to a retry-sized one."""
    from boardmodeler.authoring.backends import AuthorRequest

    calls, runner = _recording_runner(monkeypatch, [_failed_run(timed_out=True)])
    backend = build_api_backend("bob", config=AppConfig(), timeout_s=0.05)
    assert isinstance(backend, BobShellBackend)
    backend.runner = runner

    result = backend.author(
        AuthorRequest(prompt="author a model", workdir=tmp_path, model_dir=tmp_path, max_turns=1)
    )

    assert len(calls) == 1
    assert calls[0]["timeout_s"] == 0.05
    # A run the clock stopped is reported as the clock stopping it — never as the model's
    # verdict, which is what made the main edition's remainder-funded retry misleading.
    assert result.detail.startswith("bob_shell_timeout"), result.detail
