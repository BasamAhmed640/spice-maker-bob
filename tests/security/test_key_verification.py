from __future__ import annotations

import threading

import pytest

from boardmodeler.agent_providers import require
from boardmodeler.security.key_verification import verify_key
from boardmodeler.security.subprocess_guard import GuardedProcess

SECRET = "test-secret-never-log"


def test_bob_uses_exact_supplied_key_and_disables_tools(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "bob")
    monkeypatch.setenv("UNRELATED_PRIVATE_TOKEN", "do-not-pass")

    def runner(argv, **kw):
        assert SECRET not in " ".join(argv)
        assert kw["env"]["BOB_API_KEY"] == SECRET
        assert "UNRELATED_PRIVATE_TOKEN" not in kw["env"]
        assert kw["timeout_s"] == 15
        assert "read,edit,execute,mcp,skill,todo,subagent,mode" in argv
        assert argv[argv.index("--workspace") + 1] == str(kw["cwd"].resolve())
        assert kw["input_text"].startswith("Connection check.")
        assert list(kw["cwd"].iterdir()) == []
        return GuardedProcess(0, '{"status":"success","last_message":"OK"}', "", 1, False)

    monkeypatch.setattr("boardmodeler.authoring.backends.run_bob_shell", runner)
    assert verify_key(require("bob"), SECRET).status == "verified"


@pytest.mark.parametrize(
    ("stderr", "timed_out", "status", "message"),
    [
        ("401 unauthorized", False, "rejected", "rejected"),
        ("A license agreement is required", False, "unverified", "license"),
        ("", True, "unverified", "timed out"),
        ("unknown error", False, "unverified", "setup"),
    ],
)
def test_bob_failures_are_honest_and_never_echo_provider_text(
    monkeypatch, stderr, timed_out, status, message
):
    monkeypatch.setattr("shutil.which", lambda _: "bob")
    monkeypatch.setattr(
        "boardmodeler.authoring.backends.run_bob_shell",
        lambda *a, **kw: GuardedProcess(1, "", stderr + SECRET, 1, timed_out),
    )
    result = verify_key(require("bob"), SECRET)
    assert result.status == status
    assert message in result.detail
    assert SECRET not in repr(result)


def test_missing_bob_and_cancellation_do_not_send_anything(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert "Install" in verify_key(require("bob"), SECRET).detail
    event = threading.Event()
    event.set()
    assert "cancelled" in verify_key(require("bob"), SECRET, cancel=event).detail
