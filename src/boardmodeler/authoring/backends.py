"""Author backends: the agent is a *proposal* source, never a verdict source.

Honesty rule implemented here: a backend can only write files into its sandbox.
Nothing it prints — a self-reported success, a token count, a status string — is
ever treated as evidence. The loop re-runs the real simulator on the files that
were actually written, and only an observed ``.raw``/``.log`` pair can produce a
PASS. A backend that cannot run reports *why* (``availability``) and the loop
returns BLOCKED instead of silently substituting another author.

Bob Shell facts used here are the documented non-interactive interface:
``bob run [options] [prompt...]`` with ``--format json`` and ``--max-turns``;
``--format json`` prints one ``{"type": "result", ..., "stats": {...}}`` object.
The API key is passed only through the child environment (never in argv, never
logged) and is redacted from every detail/exception text this module produces.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from boardmodeler.agent_providers import AgentProvider
from boardmodeler.security.credentials import (
    Credential,
    SecretSource,
    env_var_name,
    get_credential,
    redact,
)
from boardmodeler.security.subprocess_guard import GuardedProcess, check_argv

__all__ = [
    "BOB_API_KEY_ENV",
    "BOB_CREDENTIALS_UNAVAILABLE",
    "BOB_NOT_INSTALLED",
    "DEFAULT_TIMEOUT_S",
    "AuthorBackend",
    "AuthorRequest",
    "AuthorResult",
    "BobShellBackend",
    "ProcessRunner",
    "ScriptedBackend",
    "UnavailableBackend",
    "build_agent_backend",
    "credential_for",
    "env_sources",
    "parse_result_object",
    "run_bob_shell",
    "stdout_tail",
]

DEFAULT_TIMEOUT_S = 600.0
"""One agent turn may take this long.

The authoring loop hands this to the backend when neither the caller nor the settings
name a limit; a build that hung would otherwise never return.
"""

BOB_EXECUTABLE = "bob"
BOB_API_KEY_ENV = "BOB_API_KEY"
"""Environment variable Bob Shell reads its own credential from."""

BOB_CREDENTIAL_NAME = "bob_shell"
"""Name used with the repo keyring / ``BOARDMODELER_*_API_KEY`` helpers."""

BOB_ALLOWED_BASENAMES = ("bob", "bob.exe", "bob.cmd", "bob.bat", "bob.ps1")
"""Basenames the subprocess guard accepts for the Bob Shell launcher."""

BOB_NOT_INSTALLED = (
    "bob_shell_not_installed: install from "
    "https://bob.ibm.com/docs/shell/getting-started/install-and-setup"
)
BOB_CREDENTIALS_UNAVAILABLE = (
    "bob_credentials_unavailable: set BOB_API_KEY (Scope=Inference) or store it in the keyring"
)

STDOUT_TAIL_LINES = 40
"""Lines of child output kept in :class:`AuthorResult.stdout_tail`."""

TEXT_ONLY_INSTRUCTION = "Reply with the answer text itself; do not create or modify any file."
"""Prompt line appended for an ``expect_text`` turn (Bob Shell has no other way to ask)."""

_TOKEN_STAT_KEYS = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_POLL_S = 0.2


@dataclass(frozen=True)
class AuthorRequest:
    """One authoring turn: what the agent is told and where it may write.

    ``expect_text`` asks for a *read-only* turn whose answer is the reply text
    itself: the API-key backend then writes no file at all and returns the text in
    :attr:`AuthorResult.stdout_tail`. Backends whose process decides for itself
    what to write (Bob Shell) treat it as advice: the prompt asks for text only,
    but the files a CLI writes are still whatever it wrote.
    """

    prompt: str
    workdir: Path
    model_dir: Path
    max_turns: int
    expect_text: bool = False


@dataclass(frozen=True)
class AuthorResult:
    """What a backend reports back.

    ``detail`` is short and never contains a secret; ``usage`` is in
    provider-native units (tokens for Bob Shell) and is never converted.
    """

    ok: bool
    detail: str
    usage: dict[str, float]
    stdout_tail: str
    session_id: str | None


@runtime_checkable
class AuthorBackend(Protocol):
    """Anything that can author a model file and say whether it can run at all."""

    name: str

    def availability(self) -> tuple[bool, str]:
        """``(usable, reason)``; ``reason`` is specific when ``usable`` is False."""
        ...

    def author(
        self,
        request: AuthorRequest,
        cancel: threading.Event | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AuthorResult:
        """Run one authoring turn. Implementations never raise for a failed turn.

        ``timeout_s`` bounds this one invocation when the caller has a budget;
        ``None`` leaves the backend's own configured limit in place.
        """
        ...


class ProcessRunner(Protocol):
    """How a backend launches the agent process (injectable for tests)."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        env: Mapping[str, str],
        cancel: threading.Event | None = None,
    ) -> GuardedProcess:
        """Run ``argv`` in ``cwd``; kill the tree on timeout or cancellation."""
        ...


def stdout_tail(text: str, *, lines: int = STDOUT_TAIL_LINES, secrets: Iterable[str] = ()) -> str:
    """The last ``lines`` lines of ``text``, with every secret removed."""
    tail = "\n".join(text.splitlines()[-lines:])
    return redact(tail, secrets)


def parse_result_object(stdout: str) -> dict[str, object] | None:
    """The documented Bob Shell JSON result object, taken from the *last* JSON line.

    A preamble may precede the payload, so lines are scanned from the end; the
    first object carrying ``status``/``stats`` wins, otherwise the last object
    that parsed at all is returned (so the caller can report what it saw).
    """
    fallback: dict[str, object] | None = None
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if fallback is None:
            fallback = payload
        if "status" in payload or "stats" in payload:
            return payload
    return fallback


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            shell=False,
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover - exercised on POSIX CI only
        with contextlib.suppress(OSError, AttributeError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()


def run_bob_shell(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str],
    cancel: threading.Event | None = None,
) -> GuardedProcess:
    """Run a list argv with ``shell=False``, honouring timeouts *and* cancellation.

    The executable is validated against the repo allowlist (Bob's launchers are
    accepted via ``extra_allowed``); the whole process tree is killed on timeout
    or when ``cancel`` is set. The child never shares the caller's stdin.
    """
    executable = check_argv(argv, extra_allowed=BOB_ALLOWED_BASENAMES)
    working_dir = Path(cwd).expanduser().resolve()
    if not working_dir.is_dir():
        raise NotADirectoryError(f"working directory {working_dir} does not exist")
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
    start = time.monotonic()
    deadline = start + timeout_s
    process = subprocess.Popen(
        [str(executable), *argv[1:]],
        cwd=str(working_dir),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    timed_out = False
    killed = False
    stdout = ""
    stderr = ""
    while True:
        if cancel is not None and cancel.is_set():
            killed = True
            _kill_tree(process)
        remaining = deadline - time.monotonic()
        if not killed and remaining <= 0:
            timed_out = True
            killed = True
            _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_POLL_S if not killed else 30)
            break
        except subprocess.TimeoutExpired:
            continue
    return GuardedProcess(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        wall_s=time.monotonic() - start,
        timed_out=timed_out,
    )


class BobShellBackend:
    """The real backend: ``bob run --format json`` in the agent's workdir.

    The credential is looked up through the repo helpers first (keyring entry,
    then ``BOARDMODELER_BOB_SHELL_API_KEY``) and then as a plain ``BOB_API_KEY``
    environment variable. It is only ever placed in the child *environment*.

    ``timeout_s`` is ``None`` by default: the agent runs until it is done and the
    build stops on its own progress rule instead of a clock. A caller that wants
    one invocation bounded passes a positive number, and the runner's timeout
    path applies exactly as before.
    """

    name = BOB_CREDENTIAL_NAME

    def __init__(
        self,
        *,
        team_id: str | None = None,
        env: Mapping[str, str] | None = None,
        runner: ProcessRunner | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0 or None, got {timeout_s}")
        self.team_id = team_id
        self.env = dict(env) if env is not None else None
        self.runner: ProcessRunner = runner if runner is not None else run_bob_shell
        self.timeout_s = None if timeout_s is None else float(timeout_s)

    # ------------------------------------------------------------- contract

    def availability(self) -> tuple[bool, str]:
        """Is ``bob`` on PATH *and* is any credential configured? Never guesses."""
        executable = shutil.which(BOB_EXECUTABLE)
        if executable is None:
            return False, BOB_NOT_INSTALLED
        value, source = self._credential()
        if value is None:
            return False, BOB_CREDENTIALS_UNAVAILABLE
        return True, f"bob shell at {executable}; credential source: {source}"

    def argv(self, request: AuthorRequest) -> list[str]:
        """The documented command line; the API key is *never* an argument.

        ``expect_text`` only changes the prompt (Bob's own files are still whatever
        it wrote); the argv shape, ``--max-turns`` and the prompt position do not.
        """
        executable = shutil.which(BOB_EXECUTABLE) or BOB_EXECUTABLE
        argv = [
            str(executable),
            "run",
            "--format",
            "json",
            "--max-turns",
            str(int(request.max_turns)),
        ]
        if self.team_id:
            argv.extend(["--team-id", self.team_id])
        prompt = request.prompt
        if request.expect_text:
            prompt = f"{prompt.rstrip()}\n\n{TEXT_ONLY_INSTRUCTION}"
        argv.append(prompt)
        return argv

    def author(
        self,
        request: AuthorRequest,
        cancel: threading.Event | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AuthorResult:
        """One Bob Shell run. Failures are returned, never raised with a secret."""
        if cancel is not None and cancel.is_set():
            return self._failed("cancelled: bob run was not started, the build was cancelled")
        if shutil.which(BOB_EXECUTABLE) is None:
            return self._failed(BOB_NOT_INSTALLED)
        key, _ = self._credential()
        if key is None:
            return self._failed(BOB_CREDENTIALS_UNAVAILABLE)

        argv = self.argv(request)
        child_env = dict(self.env if self.env is not None else os.environ)
        child_env[BOB_API_KEY_ENV] = key
        limit = self.timeout_s if timeout_s is None else float(timeout_s)
        if limit is not None and limit <= 0:
            return self._failed(f"bob_shell_timeout: timeout_s must be > 0, got {limit}")
        # The runner takes a number, and an infinite deadline is one that never
        # arrives — that is what ``timeout_s=None`` (no limit) means here.
        runner_timeout = float("inf") if limit is None else limit
        try:
            process = self.runner(
                argv,
                cwd=Path(request.workdir),
                timeout_s=runner_timeout,
                env=child_env,
                cancel=cancel,
            )
        except Exception as exc:
            return self._failed(redact(f"bob_shell_failed: {type(exc).__name__}: {exc}", [key]))

        tail = stdout_tail(process.stdout, secrets=[key])
        if process.timed_out:
            detail = (
                "bob_shell_timeout: bob run was stopped by its runner, but no turn timeout "
                "is configured"
                if limit is None
                else f"bob_shell_timeout: bob run did not finish within {limit:g} s"
            )
            return AuthorResult(
                ok=False, detail=detail, usage={}, stdout_tail=tail, session_id=None
            )
        if cancel is not None and cancel.is_set():
            return AuthorResult(
                ok=False,
                detail="cancelled: bob run was terminated on request",
                usage={},
                stdout_tail=tail,
                session_id=None,
            )

        payload = parse_result_object(process.stdout)
        if payload is None:
            return AuthorResult(
                ok=False,
                detail=redact(
                    "bob_shell_output_unparsed: no JSON result object on stdout "
                    f"(exit {process.returncode}); stderr: {_oneline(process.stderr)[:200]}",
                    [key],
                ),
                usage={},
                stdout_tail=tail,
                session_id=None,
            )

        stats = payload.get("stats")
        stats = stats if isinstance(stats, Mapping) else {}
        usage = {
            name: float(stats[name])
            for name in _TOKEN_STAT_KEYS
            if isinstance(stats.get(name), (int, float)) and not isinstance(stats.get(name), bool)
        }
        task_id = stats.get("task_id")
        session_id = task_id if isinstance(task_id, str) and task_id else None
        status = payload.get("status")
        ok = status == "success"
        detail = f"bob_shell: status={status!r} task={session_id or 'unknown'}"
        if "total_tokens" in usage:
            detail += f" total_tokens={int(usage['total_tokens'])}"
        if not ok:
            detail += f"; exit={process.returncode}; stderr: {_oneline(process.stderr)[:200]}"
        return AuthorResult(
            ok=ok,
            detail=redact(detail, [key]),
            usage=usage,
            stdout_tail=tail,
            session_id=session_id,
        )

    # ------------------------------------------------------------ internals

    def _credential(self) -> tuple[str | None, str]:
        """``(value, source description)``; the description never holds the value."""
        credential = get_credential(BOB_CREDENTIAL_NAME)
        if credential.value:
            return credential.value, credential.detail
        for source in (self.env, os.environ):
            value = source.get(BOB_API_KEY_ENV) if source else None
            if value:
                return value, f"{BOB_API_KEY_ENV} environment variable is set"
        return None, (
            f"{credential.detail}; neither {env_var_name(BOB_CREDENTIAL_NAME)} nor "
            f"{BOB_API_KEY_ENV} is set"
        )

    def _failed(self, detail: str) -> AuthorResult:
        return AuthorResult(ok=False, detail=detail, usage={}, stdout_tail="", session_id=None)


class ScriptedBackend:
    """A deterministic test double that plays a recorded agent.

    ``script(turn, workdir, prompt)`` is called once per turn with ``turn``
    starting at 1; whatever it writes to ``workdir`` is what the harness sees.
    The turn number and prompt are exposed so tests can prove that harness
    feedback actually reaches the next turn.

    A *text* turn (:attr:`AuthorRequest.expect_text`, the read-only reinforcement
    question) is answered with no text and does not run the script or consume a
    turn number: this double writes model files by construction, so pretending it
    had prose to give would be a lie and would shift every authoring turn number.
    """

    def __init__(self, script: Callable[[int, Path, str], None], name: str = "scripted") -> None:
        if not callable(script):
            raise TypeError(f"script must be callable, got {type(script).__name__}")
        self.script = script
        self.name = name
        self.turns = 0

    def availability(self) -> tuple[bool, str]:
        return True, "scripted backend"

    def author(
        self,
        request: AuthorRequest,
        cancel: threading.Event | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AuthorResult:
        del timeout_s
        if cancel is not None and cancel.is_set():
            return AuthorResult(
                ok=False,
                detail="cancelled: the scripted turn was not started",
                usage={},
                stdout_tail="",
                session_id=None,
            )
        if request.expect_text:
            return AuthorResult(
                ok=True,
                detail="scripted text turn: this double has no text to give",
                usage={},
                stdout_tail="",
                session_id=None,
            )
        self.turns += 1
        turn = self.turns
        self.script(turn, Path(request.workdir), request.prompt)
        return AuthorResult(
            ok=True,
            detail=f"scripted turn {turn}",
            usage={"turns": 1.0},
            stdout_tail="",
            session_id=f"scripted-{turn}",
        )


class UnavailableBackend:
    """A backend this build (or this machine) cannot run, and exactly why.

    Used where a name is refused — an unknown ``backend_name``, a provider id this
    build does not accept, a config file that does not load. It is never a silent
    substitute for a working backend: ``availability()`` reports the reason and the
    loop stops with ``BLOCKED`` naming it (D-011, AGENTS rule 5).
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def availability(self) -> tuple[bool, str]:
        return False, self.reason

    def author(
        self,
        request: AuthorRequest,
        cancel: threading.Event | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AuthorResult:
        del request, cancel, timeout_s
        return AuthorResult(ok=False, detail=self.reason, usage={}, stdout_tail="", session_id=None)


def build_agent_backend(
    *,
    provider_id: str | None = None,
    team_id: str | None = None,
    timeout_s: float | None = None,
) -> AuthorBackend:
    """The agent backend for this build's catalog: the provider it accepts.

    ``provider_id`` is the id exactly as configured; one this build does not accept is
    refused **by name** (``api_provider_unavailable: ...``) rather than substituted, so a
    hand-edited config can only ever produce an honest block. The catalog is the single
    source of which providers exist: an entry whose transport this build has no backend
    for is refused too, never coerced onto another one.
    """
    from boardmodeler import agent_providers

    provider = (
        agent_providers.by_id(provider_id) if provider_id else agent_providers.default_provider()
    )
    if provider is None:
        accepted = ", ".join(repr(name) for name in agent_providers.ids())
        return UnavailableBackend(
            str(provider_id),
            f"api_provider_unavailable: {provider_id!r} is not a provider this build accepts; "
            f"this build accepts {accepted}",
        )
    if provider.wire == "bob-shell":
        return BobShellBackend(team_id=team_id, timeout_s=timeout_s)
    return UnavailableBackend(
        provider.id,
        f"wire_unsupported: {provider.wire!r} has no backend in this build; "
        "this build reaches its provider through the Bob CLI",
    )


def env_sources(provider: AgentProvider) -> tuple[str, ...]:
    """Every environment variable a key for ``provider`` may come from, in order.

    The first entry is the ``BOARDMODELER_<NAME>_API_KEY`` fallback the shared
    credential helper reads; the rest are the vendor's own variables declared by
    the catalog. :func:`credential_for` and the missing-key reason both read this
    one tuple, so a message can never advertise a variable nothing reads.
    """
    return (env_var_name(provider.credential), *provider.env_aliases)


def credential_for(
    provider: AgentProvider, lookup: Callable[[str], Credential] | None = None
) -> Credential:
    """The key for ``provider``: keyring, ``BOARDMODELER_<NAME>_API_KEY``, then aliases.

    ``lookup`` is the repo helper (:func:`boardmodeler.security.credentials.get_credential`
    by default, and the injectable seam tests use); the catalog's own environment
    variables are read here, in order, and the matching variable is named in the
    returned ``detail``. Public because ``doctor`` must report the *same*
    resolution the backend performs — a doctor that contradicts a working build is
    worse than no doctor. Nothing here ever puts a value into a returned text.
    """
    credential = (lookup or get_credential)(provider.credential)
    if credential.value:
        return credential
    for variable in env_sources(provider)[1:]:
        value = os.environ.get(variable)
        if value:
            return Credential(
                name=provider.credential,
                value=value,
                source=SecretSource.ENV,
                detail=f"environment variable {variable}",
            )
    return credential
