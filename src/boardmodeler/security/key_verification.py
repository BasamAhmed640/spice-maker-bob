"""Bounded credential checks. No document, key value or provider response is logged."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from boardmodeler.agent_providers import AgentProvider, by_id

CHECK_TIMEOUT_S = 15.0
CHECK_PROMPT = "Connection check. Reply only with OK. Do not use tools or read files."


@dataclass(frozen=True)
class KeyVerification:
    status: str
    detail: str


def verify_key(
    provider: AgentProvider,
    key: str,
    *,
    model: str | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
    cancel: threading.Event | None = None,
) -> KeyVerification:
    """Check the supplied key, never another saved key; indeterminate is not invalid."""
    if by_id(provider.id) != provider or not key.strip() or timeout_s <= 0:
        return KeyVerification("unverified", "Select a supported provider and enter a key.")
    if cancel is not None and cancel.is_set():
        return KeyVerification("unverified", "Check cancelled.")
    try:
        if provider.uses_cli:
            return _verify_bob(key, timeout_s, cancel)
        from boardmodeler.authoring.api_backend import verify_http_key

        return verify_http_key(provider, key, model=model, timeout_s=timeout_s)
    except Exception:
        # Exception strings can contain request headers or echoed secrets.
        return KeyVerification(
            "unverified", "Could not verify; check connection and provider setup."
        )


def _verify_bob(key: str, timeout_s: float, cancel: threading.Event | None) -> KeyVerification:
    from boardmodeler.authoring.backends import parse_result_object, run_bob_shell

    executable = shutil.which("bob")
    if executable is None:
        return KeyVerification("unverified", "Install IBM Bob Shell, then reopen SETUP and retry.")
    env = dict(os.environ)
    env["BOB_API_KEY"] = key
    with tempfile.TemporaryDirectory(prefix="spice-key-check-") as folder:
        process = run_bob_shell(
            [
                executable,
                "run",
                "--format",
                "json",
                "--mode",
                "ask",
                "--max-turns",
                "1",
                "--max-cost",
                "0.05",
                "--disable-mcp",
                "--disable-subagents",
                "--disable-tool-groups",
                "read,edit,execute,mcp,skill,todo,subagent,mode",
            ],
            cwd=Path(folder),
            timeout_s=timeout_s,
            env=env,
            cancel=cancel,
            input_text=CHECK_PROMPT,
        )
    if process.timed_out or (cancel is not None and cancel.is_set()):
        return KeyVerification("unverified", "Check timed out; the saved key may still be valid.")
    payload = parse_result_object(process.stdout)
    if (
        process.returncode == 0
        and payload
        and payload.get("status") == "success"
        and isinstance(payload.get("last_message"), str)
        and payload["last_message"].strip()
    ):
        return KeyVerification("verified", "IBM Bob answered the connection check.")
    # Inspect only to choose a fixed message. Never return stdout/stderr or a key fragment.
    failure = (process.stdout + process.stderr).lower()
    if "license agreement" in failure:
        return KeyVerification(
            "unverified", "Open IBM Bob Shell once to review and accept its license."
        )
    if any(
        word in failure for word in ("unauthorized", "invalid api key", "invalid_api_key", "401")
    ):
        return KeyVerification("rejected", "IBM Bob rejected this key. Check its value and scope.")
    if "team" in failure:
        return KeyVerification("unverified", "Use an IBM Bob Inference key for this setup.")
    return KeyVerification(
        "unverified", "Bob could not complete the check; check Shell setup and quota."
    )
