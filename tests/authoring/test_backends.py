"""Author backends: argv shape, stdout parsing, availability, and secret hygiene.

Everything is hermetic: no network, no Bob installation. The process runner is
injected except in two Windows tests that drive a real ``bob.cmd`` shim, which
is what proves the timeout/cancel paths actually kill the child tree.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import pytest

from boardmodeler.authoring import backends
from boardmodeler.authoring.backends import (
    BOB_CREDENTIALS_UNAVAILABLE,
    BOB_NOT_INSTALLED,
    AuthorRequest,
    BobShellBackend,
    ScriptedBackend,
    parse_result_object,
)
from boardmodeler.security.credentials import Credential, SecretSource
from boardmodeler.security.paths import PathGuardError
from boardmodeler.security.subprocess_guard import GuardedProcess

SENTINEL_KEY = "bob-sentinel-key-9f3ac2"
PROMPT = "author a model for U1"
EXE = r"C:\tools\bob\bob.exe"
TIMEOUT_S = 5.0


def request(tmp_path: Path, *, max_turns: int = 5) -> AuthorRequest:
    return AuthorRequest(
        prompt=PROMPT,
        workdir=tmp_path,
        model_dir=tmp_path / "model",
        max_turns=max_turns,
    )


def payload_line(
    *, status: str = "success", task_id: str | None = "task-42", **stats: object
) -> str:
    values: dict[str, object] = {
        "task_id": task_id,
        "total_tokens": 1234,
        "input_tokens": 1000,
        "output_tokens": 234,
        "cache_read_tokens": 10,
        "cache_write_tokens": 4,
        "cache_ratio": 0.012,
    }
    values.update(stats)
    return json.dumps(
        {
            "type": "result",
            "timestamp": "2026-09-18T10:00:00Z",
            "status": status,
            "stats": values,
        }
    )


class Recorder:
    """A ``ProcessRunner`` that never starts a process."""

    def __init__(self, result: GuardedProcess) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        argv,
        *,
        cwd: Path,
        timeout_s: float,
        env,
        cancel: threading.Event | None = None,
        input_text: str | None = None,
    ) -> GuardedProcess:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": Path(cwd),
                "timeout_s": timeout_s,
                "env": dict(env),
                "cancel": cancel,
                "input_text": input_text,
            }
        )
        return self.result


def test_text_reply_uses_last_message_and_never_resumes(tmp_path, keyed):
    from dataclasses import replace

    payload = json.loads(payload_line())
    payload["last_message"] = '{"answer": "extracted"}'
    runner = Recorder(completed(json.dumps(payload)))
    backend = BobShellBackend(runner=runner)
    text_result = backend.author(
        replace(request(tmp_path), expect_text=True, session_id="must-not-resume")
    )
    assert json.loads(text_result.stdout_tail) == {"answer": "extracted"}
    assert "--resume" not in runner.calls[0]["argv"]
    backend.author(replace(request(tmp_path), session_id="task-42"))
    argv = runner.calls[1]["argv"]
    assert "--resume" not in argv
    assert PROMPT in runner.calls[1]["input_text"]


def completed(stdout: str = "", *, returncode: int = 0, timed_out: bool = False, stderr: str = ""):
    return GuardedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        wall_s=0.01,
        timed_out=timed_out,
    )


@pytest.fixture
def bob_env(monkeypatch: pytest.MonkeyPatch) -> Credential:
    """Bob "installed", no credential anywhere, so lookups are deterministic."""
    monkeypatch.setattr(backends.shutil, "which", lambda name: EXE if name == "bob" else None)
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOARDMODELER_BOB_SHELL_API_KEY", raising=False)
    credential = Credential(
        name="bob_shell",
        value=None,
        source=SecretSource.MISSING,
        detail="no keyring value for key 'provider:bob_shell:api_key'",
    )
    monkeypatch.setattr(backends, "get_credential", lambda name, **kwargs: credential)
    return credential


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch, bob_env: Credential) -> str:
    """Same, but a credential with the sentinel value is available."""
    monkeypatch.setattr(
        backends,
        "get_credential",
        lambda name, **kwargs: Credential(
            name=name,
            value=SENTINEL_KEY,
            source=SecretSource.ENV,
            detail="environment variable BOARDMODELER_BOB_SHELL_API_KEY",
        ),
    )
    return SENTINEL_KEY


# --------------------------------------------------------------------- argv


def test_argv_matches_the_documented_bob_run_shape(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    recorder = Recorder(completed(payload_line() + "\n"))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path, max_turns=5))

    assert authored.ok is True
    assert recorder.calls[0]["argv"] == [
        EXE,
        "run",
        "--workspace",
        str(tmp_path.resolve()),
        "--mode",
        "ask",
        "--format",
        "json",
        "--max-turns",
        "5",
        "--disable-mcp",
        "--disable-subagents",
        "--disable-tool-groups",
        "read,edit,execute,mcp,skill,todo,subagent,mode",
    ]
    assert recorder.calls[0]["cwd"] == tmp_path
    assert PROMPT in recorder.calls[0]["input_text"]
    assert recorder.calls[0]["timeout_s"] == TIMEOUT_S
    assert recorder.calls[0]["env"]["BOB_API_KEY"] == SENTINEL_KEY
    assert SENTINEL_KEY not in " ".join(recorder.calls[0]["argv"])
    assert set(recorder.calls[0]["env"]) == {
        "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TEMP", "TMP", "TMPDIR", "BOB_API_KEY",
    }


def test_team_id_is_added_only_when_configured(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    recorder = Recorder(completed(payload_line()))
    backend = BobShellBackend(
        team_id="team-7", env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S
    )

    assert backend.author(request(tmp_path)).ok is True

    assert recorder.calls[0]["argv"] == [
        EXE,
        "run",
        "--workspace",
        str(tmp_path.resolve()),
        "--mode",
        "ask",
        "--format",
        "json",
        "--max-turns",
        "5",
        "--team-id",
        "team-7",
        "--disable-mcp",
        "--disable-subagents",
        "--disable-tool-groups",
        "read,edit,execute,mcp,skill,todo,subagent,mode",
    ]


def test_tool_free_bob_reply_is_written_only_by_application(tmp_path, keyed) -> None:
    from dataclasses import replace

    payload = json.loads(payload_line())
    payload["last_message"] = "```spice\n.subckt DEMO IN OUT\nR1 IN OUT 1k\n.ends DEMO\n```"
    runner = Recorder(completed(json.dumps(payload)))
    backend = BobShellBackend(runner=runner)
    result = backend.author(replace(request(tmp_path), subckt="DEMO"))
    assert result.ok
    assert (tmp_path / "model" / "DEMO.lib").read_text(encoding="utf-8").startswith(".subckt DEMO")
    argv = runner.calls[0]["argv"]
    groups = argv[argv.index("--disable-tool-groups") + 1].split(",")
    assert {"read", "edit", "execute"}.issubset(groups)


def test_tool_free_bob_reply_rejects_external_file_directive(tmp_path, keyed) -> None:
    from dataclasses import replace

    payload = json.loads(payload_line())
    payload["last_message"] = ".subckt DEMO IN OUT\n.include C:/private/file.lib\n.ends DEMO"
    backend = BobShellBackend(runner=Recorder(completed(json.dumps(payload))))
    result = backend.author(replace(request(tmp_path), subckt="DEMO"))
    assert not result.ok and "unsupported model directive" in result.detail
    assert not (tmp_path / "model" / "DEMO.lib").exists()


def test_tool_free_bob_reply_rejects_scopedata_file(tmp_path, keyed) -> None:
    from dataclasses import replace

    payload = json.loads(payload_line())
    payload["last_message"] = (
        ".subckt DEMO IN OUT\nV1 IN OUT PWL(SCOPEDATA=C:/private/data.txt)\n.ends DEMO"
    )
    backend = BobShellBackend(runner=Recorder(completed(json.dumps(payload))))
    result = backend.author(replace(request(tmp_path), subckt="DEMO"))
    assert not result.ok and "external file reference" in result.detail
    assert not (tmp_path / "model" / "DEMO.lib").exists()


def test_tool_free_bob_reply_accepts_helper_subcircuits(tmp_path, keyed) -> None:
    from dataclasses import replace

    payload = json.loads(payload_line())
    payload["last_message"] = (
        ".model DS D(Is=1e-12)\n"
        ".subckt HELPER A B\nD1 A B DS\n.ends HELPER\n"
        ".subckt DEMO IN OUT\nX1 IN OUT HELPER\n.ends DEMO"
    )
    backend = BobShellBackend(runner=Recorder(completed(json.dumps(payload))))
    result = backend.author(replace(request(tmp_path), subckt="DEMO"))
    assert result.ok
    assert "X1 IN OUT HELPER" in (tmp_path / "model" / "DEMO.lib").read_text(encoding="utf-8")


# ------------------------------------------------------------ stdout parsing


def test_last_json_line_is_parsed_and_stats_are_reported(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    stdout = 'bob 1.2.3 starting\n{"note": "preamble"}\n' + payload_line() + "\n"
    recorder = Recorder(completed(stdout))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path))

    assert authored.ok is True
    assert authored.session_id == "task-42"
    assert authored.usage == {
        "total_tokens": 1234.0,
        "input_tokens": 1000.0,
        "output_tokens": 234.0,
        "cache_read_tokens": 10.0,
        "cache_write_tokens": 4.0,
    }
    assert authored.detail.startswith("bob_shell: status='success'")
    assert "total_tokens=1234" in authored.detail


def test_an_error_payload_is_not_ok(bob_env: Credential, keyed: str, tmp_path: Path) -> None:
    recorder = Recorder(completed(payload_line(status="error", task_id="task-9") + "\n"))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path))

    assert authored.ok is False
    assert "status='error'" in authored.detail
    assert authored.session_id == "task-9"


def test_stdout_without_a_json_object_is_unparsed(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    recorder = Recorder(completed("bob: no json here\n", returncode=1, stderr="boom"))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path))

    assert authored.ok is False
    assert authored.detail.startswith("bob_shell_output_unparsed")
    assert authored.session_id is None
    assert parse_result_object("no json at all") is None


def test_a_timeout_reports_the_timeout_reason(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    recorder = Recorder(completed("partial", returncode=-1, timed_out=True))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path))

    assert authored.ok is False
    assert authored.detail.startswith("bob_shell_timeout")
    assert "5 s" in authored.detail


def test_a_backend_without_a_timeout_never_bounds_the_run(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    """``timeout_s=None`` is the default: the agent runs until it is done."""
    recorder = Recorder(completed(payload_line() + "\n"))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder)

    authored = backend.author(request(tmp_path))

    assert backend.timeout_s is None
    assert authored.ok is True
    assert math.isinf(recorder.calls[0]["timeout_s"]), "no deadline is an infinite one"


def test_a_non_positive_timeout_is_still_refused() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        BobShellBackend(timeout_s=0.0)
    with pytest.raises(ValueError, match="timeout_s"):
        BobShellBackend(timeout_s=-1.0)


def test_a_stop_without_a_configured_timeout_says_so(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    """A runner that stopped the child itself cannot blame a limit that is not set."""
    recorder = Recorder(completed("partial", returncode=-1, timed_out=True))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder)

    authored = backend.author(request(tmp_path))

    assert authored.ok is False
    assert authored.detail.startswith("bob_shell_timeout")
    assert "no turn timeout is configured" in authored.detail


def test_a_set_cancel_event_stops_before_launching(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    recorder = Recorder(completed(payload_line()))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)
    cancel = threading.Event()
    cancel.set()

    authored = backend.author(request(tmp_path), cancel)

    assert authored.ok is False
    assert authored.detail.startswith("cancelled")
    assert recorder.calls == []


def test_cancellation_while_running_reports_cancelled(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    class Cancelling(Recorder):
        def __call__(self, argv, *, cwd, timeout_s, env, cancel=None, input_text=None):
            assert cancel is not None
            cancel.set()
            return completed(payload_line())

    backend = BobShellBackend(
        env={"PATH": "x"}, runner=Cancelling(completed()), timeout_s=TIMEOUT_S
    )

    authored = backend.author(request(tmp_path), threading.Event())

    assert authored.ok is False
    assert authored.detail.startswith("cancelled")


# ------------------------------------------------------------- availability


def test_availability_reports_that_bob_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)

    usable, reason = BobShellBackend().availability()

    assert usable is False
    assert reason == BOB_NOT_INSTALLED
    assert reason.startswith("bob_shell_not_installed:")


def test_availability_reports_missing_credentials(bob_env: Credential) -> None:
    usable, reason = BobShellBackend(env={}).availability()

    assert usable is False
    assert reason == BOB_CREDENTIALS_UNAVAILABLE
    assert reason.startswith("bob_credentials_unavailable:")
    assert reason != BOB_NOT_INSTALLED


def test_availability_is_true_with_a_credential(keyed: str) -> None:
    usable, reason = BobShellBackend(env={}).availability()

    assert usable is True
    assert "bob shell at" in reason
    assert SENTINEL_KEY not in reason


def test_a_plain_bob_api_key_in_the_environment_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backends.shutil, "which", lambda name: EXE if name == "bob" else None)
    monkeypatch.setattr(
        backends,
        "get_credential",
        lambda name, **kwargs: Credential(
            name=name, value=None, source=SecretSource.MISSING, detail="no keyring entry"
        ),
    )
    monkeypatch.setenv("BOB_API_KEY", SENTINEL_KEY)

    usable, reason = BobShellBackend(env={}).availability()

    assert usable is True
    assert "BOB_API_KEY" in reason
    assert SENTINEL_KEY not in reason


def test_an_unavailable_backend_refuses_to_author(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    backend = BobShellBackend(runner=Recorder(completed(payload_line())))

    authored = backend.author(request(tmp_path))

    assert authored.ok is False
    assert authored.detail == BOB_NOT_INSTALLED


# ------------------------------------------------------------ secret hygiene


def test_the_api_key_never_appears_in_results(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    stdout = payload_line() + f"\nnote: credential was {SENTINEL_KEY}\n"
    stderr = f"warning: {SENTINEL_KEY} expires soon"
    recorder = Recorder(completed(stdout, stderr=stderr))
    backend = BobShellBackend(env={"PATH": "x"}, runner=recorder, timeout_s=TIMEOUT_S)

    authored = backend.author(request(tmp_path))

    assert authored.ok is True
    assert SENTINEL_KEY not in authored.detail
    assert SENTINEL_KEY not in authored.stdout_tail
    assert "[REDACTED]" in authored.stdout_tail


def test_a_raising_runner_cannot_leak_the_key(
    bob_env: Credential, keyed: str, tmp_path: Path
) -> None:
    class Exploding(Recorder):
        def __call__(self, argv, *, cwd, timeout_s, env, cancel=None, input_text=None):
            raise OSError(f"cannot start launcher using {SENTINEL_KEY}")

    backend = BobShellBackend(env={"PATH": "x"}, runner=Exploding(completed()), timeout_s=TIMEOUT_S)

    try:
        authored = backend.author(request(tmp_path))
    except Exception as exc:  # pragma: no cover - the whole point is that this cannot happen
        pytest.fail(f"author() raised instead of reporting the failure: {exc!r}")

    assert authored.ok is False
    assert authored.detail.startswith("bob_shell_failed")
    assert SENTINEL_KEY not in authored.detail
    assert "[REDACTED]" in authored.detail


def test_run_bob_shell_refuses_an_executable_outside_the_guard_allowlist(tmp_path: Path) -> None:
    with pytest.raises(PathGuardError, match="allowlist"):
        backends.run_bob_shell(
            [str(tmp_path / "evil.exe"), "run"], cwd=tmp_path, timeout_s=1.0, env={}
        )


# ------------------------------------------------- real child processes (nt)

_SLEEP_SHIM = "@echo off\r\nping -n 30 127.0.0.1 >nul\r\n"
_needs_windows = pytest.mark.skipif(os.name != "nt", reason="drives a Windows .cmd shim")


@_needs_windows
def test_run_bob_shell_returns_the_child_output(tmp_path: Path) -> None:
    shim = tmp_path / "bob.cmd"
    shim.write_text("@echo off\r\necho " + payload_line() + "\r\n", encoding="ascii")

    process = backends.run_bob_shell(
        [str(shim), "run", "--format", "json"], cwd=tmp_path, timeout_s=20.0, env=dict(os.environ)
    )

    assert process.timed_out is False
    assert process.returncode == 0
    parsed = parse_result_object(process.stdout)
    assert parsed is not None
    stats = parsed["stats"]
    assert isinstance(stats, dict)
    assert stats["task_id"] == "task-42"


@_needs_windows
def test_run_bob_shell_kills_a_hung_child_on_timeout(tmp_path: Path) -> None:
    shim = tmp_path / "bob.cmd"
    shim.write_text(_SLEEP_SHIM, encoding="ascii")

    started = time.monotonic()
    process = backends.run_bob_shell(
        [str(shim), "run"], cwd=tmp_path, timeout_s=1.0, env=dict(os.environ)
    )
    elapsed = time.monotonic() - started

    assert process.timed_out is True
    assert elapsed < 15.0


@_needs_windows
def test_run_bob_shell_kills_a_running_child_on_cancel(tmp_path: Path) -> None:
    shim = tmp_path / "bob.cmd"
    shim.write_text(_SLEEP_SHIM, encoding="ascii")
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()

    started = time.monotonic()
    try:
        process = backends.run_bob_shell(
            [str(shim), "run"],
            cwd=tmp_path,
            timeout_s=60.0,
            env=dict(os.environ),
            cancel=cancel,
        )
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()

    assert cancel.is_set()
    assert process.timed_out is False
    assert elapsed < 15.0


# ------------------------------------------------------------ scripted double


def test_scripted_backend_plays_turns_in_order(tmp_path: Path) -> None:
    seen: list[tuple[int, Path, str]] = []

    def script(turn: int, workdir: Path, prompt: str) -> None:
        seen.append((turn, workdir, prompt))
        (workdir / "model").mkdir(parents=True, exist_ok=True)
        (workdir / "model" / "U1.lib").write_text(f"# turn {turn}", encoding="utf-8")

    backend = ScriptedBackend(script)
    assert backend.availability() == (True, "scripted backend")
    assert backend.name == "scripted"

    first = backend.author(request(tmp_path))
    second = backend.author(request(tmp_path))

    assert [turn for turn, _, _ in seen] == [1, 2]
    assert [prompt for _, _, prompt in seen] == [PROMPT, PROMPT]
    assert first.ok and second.ok
    assert first.session_id == "scripted-1"
    assert second.session_id == "scripted-2"
    assert (tmp_path / "model" / "U1.lib").read_text(encoding="utf-8") == "# turn 2"


def test_scripted_backend_rejects_a_non_callable_script() -> None:
    with pytest.raises(TypeError, match="callable"):
        ScriptedBackend("not a script")  # type: ignore[arg-type]
