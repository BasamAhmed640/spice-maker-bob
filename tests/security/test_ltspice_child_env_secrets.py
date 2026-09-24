"""No agent or CI credential reaches an LTspice child process.

Checked twice: on the scrubber itself, and on the environment block handed to the OS
(``_winapi.CreateProcess``) when the simulator module really spawns, so a call site
that forgot the scrubbed environment fails here too.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from boardmodeler.simulation import ltspice

SECRET_NAMES = (
    "OPENCODE_GO_API_KEY",
    "OPENCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "BOB_API_KEY",
    "BOARDMODELER_BOB_SHELL_API_KEY",
    "BOARDMODELER_OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
)


@pytest.fixture
def poisoned(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {name: f"fake-{name.lower()}-0123456789abcdef" for name in SECRET_NAMES}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def _scrubbed() -> dict[str, str]:
    scrub = getattr(ltspice, "child_environment", None)
    if scrub is None:  # the Bob edition keeps its allowlist in storage
        from boardmodeler.storage import ltspice_environment as scrub
    return dict(scrub())


def _assert_clean(env: dict[str, str], values: dict[str, str]) -> None:
    leaked_names = sorted(n for n in env if n.upper() in SECRET_NAMES)
    leaked_values = sorted(n for n, v in env.items() if any(s in v for s in values.values()))
    assert not leaked_names and not leaked_values, (leaked_names, leaked_values)


def test_the_scrubber_drops_every_named_secret(poisoned: dict[str, str]) -> None:
    env = _scrubbed()
    assert "PATH" in {name.upper() for name in env}, "scrubber returned nothing useful"
    _assert_clean(env, poisoned)


@pytest.mark.skipif(os.name != "nt", reason="LTspice and _winapi are Windows-only")
def test_the_real_spawn_carries_no_secret(
    poisoned: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi

    seen: list[dict[str, str]] = []

    class Stop(OSError):
        pass

    def fake_create_process(app, cmd, proc_attrs, thread_attrs, inherit, flags, env, *rest):
        seen.append(dict(os.environ) if env is None else dict(env))
        raise Stop("spawn intercepted by the test")

    monkeypatch.setattr(_winapi, "CreateProcess", fake_create_process)
    exe = tmp_path / "LTspice.exe"
    exe.write_bytes(b"MZ test fixture")
    deck = tmp_path / "op.cir"
    deck.write_text("* op\nV1 1 0 1\nR1 1 0 1k\n.op\n.end\n", encoding="utf-8")
    for attempt in (
        lambda: ltspice.version(exe),
        lambda: ltspice.run_batch(exe, deck, tmp_path, timeout_s=5.0, lock_timeout_s=5.0),
    ):
        with contextlib.suppress(Exception):
            attempt()
    assert seen, "no LTspice spawn reached CreateProcess; the check would pass vacuously"
    for env in seen:
        _assert_clean(env, poisoned)
