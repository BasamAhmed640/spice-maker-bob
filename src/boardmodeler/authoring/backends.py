"""Author backends: the agent is a *proposal* source, never a verdict source.

Honesty rule implemented here: Bob Shell has no tools. It returns model text, and
the application alone validates and writes the candidate. Nothing Bob prints — a
self-reported success, a token count, a status string — is treated as evidence.
The loop runs the real simulator on the written candidate, and only observed output can produce a
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
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from boardmodeler.security.credentials import env_var_name, get_credential, redact
from boardmodeler.security.network import internet_allowed, refusal_detail, require_network
from boardmodeler.security.subprocess_guard import GuardedProcess, check_argv

__all__ = [
    "BOB_API_KEY_ENV",
    "BOB_CREDENTIALS_UNAVAILABLE",
    "BOB_NOT_INSTALLED",
    "AuthorBackend",
    "AuthorRequest",
    "AuthorResult",
    "BobShellBackend",
    "ProcessRunner",
    "ScriptedBackend",
    "UnavailableBackend",
    "parse_result_object",
    "run_bob_shell",
    "stdout_tail",
]

BOB_EXECUTABLE = "bob"
BOB_API_KEY_ENV = "BOB_API_KEY"
"""Environment variable Bob Shell reads its own credential from."""

BOB_CREDENTIAL_NAME = "bob_shell"
"""Name used with the repo's plain local credential file / ``BOARDMODELER_*_API_KEY`` helpers."""

BOB_ALLOWED_BASENAMES = ("bob", "bob.exe", "bob.cmd", "bob.bat", "bob.ps1")
"""Basenames the subprocess guard accepts for the Bob Shell launcher."""

BOB_NOT_INSTALLED = (
    "bob_shell_not_installed: install from "
    "https://bob.ibm.com/docs/shell/getting-started/install-and-setup"
)
BOB_CREDENTIALS_UNAVAILABLE = "bob_credentials_unavailable: set BOB_API_KEY (Scope=Inference) or store it in this folder's plain local file"

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
BOB_DISABLED_TOOL_GROUPS = "read,edit,execute,mcp,skill,todo,subagent,mode"
_BOB_CHILD_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "SYSTEMDRIVE",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)


@dataclass(frozen=True)
class AuthorRequest:
    """One authoring turn: what the agent is told and where it may write.

    ``expect_text`` asks for a text-only turn. For model turns, ``subckt`` names
    the candidate that the application writes after validating Bob's reply.
    """

    prompt: str
    workdir: Path
    model_dir: Path
    max_turns: int
    expect_text: bool = False
    session_id: str | None = None
    subckt: str | None = None
    progress: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    #: Per-stage reasoning effort for providers whose entry documents the switch
    #: (``reasoning_effort`` in its ``extra_body``); ``None`` keeps the entry's own value.
    reasoning_effort: str | None = None


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
        input_text: str | None = None,
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
    input_text: str | None = None,
) -> GuardedProcess:
    """Run a list argv with ``shell=False``, honouring timeouts *and* cancellation.

    The executable is validated against the repo allowlist (Bob's launchers are
    accepted via ``extra_allowed``); the whole process tree is killed on timeout
    or when ``cancel`` is set. The child never shares the caller's stdin.
    """
    require_network("IBM Bob Shell")
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
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
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
            stdout, stderr = process.communicate(
                input=input_text, timeout=_POLL_S if not killed else 30
            )
            break
        except subprocess.TimeoutExpired:
            input_text = None
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

    The credential is looked up through the repo helpers first (plain local entry,
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
        if not internet_allowed():
            return False, refusal_detail("IBM Bob Shell")
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
            "--workspace",
            str(Path(request.workdir).resolve()),
            "--mode",
            "ask",
            "--format",
            "json",
            "--max-turns",
            str(int(request.max_turns)),
        ]
        if self.team_id:
            argv.extend(["--team-id", self.team_id])
        argv.extend(
            [
                "--disable-mcp",
                "--disable-subagents",
                "--disable-tool-groups",
                BOB_DISABLED_TOOL_GROUPS,
            ]
        )
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
        if not internet_allowed():
            return self._failed(refusal_detail("IBM Bob Shell"))
        if shutil.which(BOB_EXECUTABLE) is None:
            return self._failed(BOB_NOT_INSTALLED)
        key, _ = self._credential()
        if key is None:
            return self._failed(BOB_CREDENTIALS_UNAVAILABLE)

        argv = self.argv(request)
        source_env = self.env if self.env is not None else os.environ
        child_env = {name: source_env[name] for name in _BOB_CHILD_ENV_KEYS if name in source_env}
        from boardmodeler.storage import bob_environment

        child_env = bob_environment(child_env)
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
                input_text=(
                    f"{request.prompt.rstrip()}\n\n{TEXT_ONLY_INSTRUCTION}"
                    if request.expect_text
                    else request.prompt.rstrip()
                    + "\n\nYou have no tools. Return only the complete .subckt library in one "
                    "```spice code block. The application validates and writes it, then "
                    "runs LTspice separately. Do not run commands or claim verification."
                ),
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
        message = payload.get("last_message")
        if request.expect_text:
            if not isinstance(message, str) or not message.strip():
                return self._failed("bob_text_missing: the result contained no last_message")
            tail = redact(message, [key])
        elif ok and request.subckt is not None:
            try:
                library = _extract_library(message, request.subckt)
                root = Path(request.workdir).resolve()
                model_dir = Path(request.model_dir).resolve()
                if not model_dir.is_relative_to(root):
                    raise ValueError("model directory is outside this workspace")
                model_dir.mkdir(parents=True, exist_ok=True)
                (model_dir / f"{request.subckt}.lib").write_bytes(library.encode("utf-8"))
            except (OSError, ValueError) as exc:
                return self._failed(f"bob_model_rejected: {exc}")
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


def _extract_library(message: object, subckt: str) -> str:
    """Accept one self-contained model reply; the application owns the only write."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", subckt):
        raise ValueError("invalid subcircuit name")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Bob returned no library text")
    if len(message) > 1_000_000 or "\x00" in message:
        raise ValueError("library text is too large or contains NUL")
    fence = re.fullmatch(r"\s*```(?:spice|spice3|cir|lib)?\s*\n(.*?)\n```\s*", message, re.I | re.S)
    library = fence.group(1) if fence else message.strip()
    lines = library.splitlines()
    meaningful = [
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("*", ";"))
    ]
    allowed_directives = {
        ".subckt",
        ".ends",
        ".model",
        ".param",
        ".func",
        ".if",
        ".elseif",
        ".else",
        ".endif",
        ".nodeset",
        ".ic",
        ".options",
    }
    opened: list[str] = []
    found_main = 0
    subcircuit_count = 0
    for line in meaningful:
        fields = line.split()
        first = fields[0].lower()
        if first.startswith(".") and first not in allowed_directives:
            raise ValueError(f"unsupported model directive {first}")
        if first == ".subckt":
            if len(fields) < 3 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", fields[1]):
                raise ValueError("invalid .subckt declaration")
            if opened:
                raise ValueError("nested .subckt is not supported")
            opened.append(fields[1].lower())
            subcircuit_count += 1
            found_main += fields[1].lower() == subckt.lower()
            if subcircuit_count > 64:
                raise ValueError("too many helper subcircuits")
        elif first == ".ends":
            if not opened or (len(fields) > 1 and fields[1].lower() != opened[-1]):
                raise ValueError("unmatched .ends")
            opened.pop()
        elif not first.startswith((".", "+")) and not opened:
            raise ValueError("component text outside a .subckt")
        if re.search(r"(?i)\b(?:file|wavefile|libfile|scopedata)\s*=", line):
            raise ValueError("external file reference is not allowed")
    if opened or found_main != 1:
        raise ValueError("reply must contain the requested .subckt and matched .ends")
    return library.rstrip() + "\n"


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
