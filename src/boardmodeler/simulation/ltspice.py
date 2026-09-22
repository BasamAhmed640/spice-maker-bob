"""LTspice batch invocation (D6).

Everything in the project that starts LTspice goes through this module, so the
resolved invocation is defined exactly once.

**Empirically resolved invocation** (LTspice 26.0.0.3, Windows, 2026-09-18):

* ``LTspice.exe -b <deck>`` with ``cwd`` = the deck's directory. This works for
  ``.cir``, ``.net`` **and** ``.asc`` files; the two-step ``-netlist`` path is
  only needed when a ``.net`` is wanted as an artifact. Outputs land next to the
  deck: ``<stem>.raw``, ``<stem>.log``, ``<stem>.op.raw``, ``<stem>.db``.
* ``-ascii`` before the deck selects the text ``.raw`` payload.
* ``-version`` prints ``26.0.0`` and exits 0 (0.2 s) — safe to call.
* The deck path may be relative to ``cwd``; we always pass an absolute path.
* **``-I<path>`` is not used, ever.** Measured behaviour: with ``-I`` present,
  LTspice runs a GUI-style instance that (a) does not resolve the search path
  the way the help text claims, and (b) leaves the process alive on a modal
  error dialog — the process neither exits nor times out, so a batch run hangs.
  Models are therefore resolved by absolute ``.include`` paths in ``.cir`` decks
  and by placing generated ``.asy``/``.lib`` files beside a ``.asc`` (verified:
  LTspice resolves a local symbol and emits ``.lib <model>`` for it
  automatically).
* There is no ``-o`` output-directory switch in this build.

Every run is watchdogged: the process tree of the PID **we** spawned is killed
once the log shows ``Total elapsed time`` and the process still has not exited
after a grace period, or once the timeout elapses. Nothing is ever reported as
having run when it did not.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from boardmodeler.domain.hashing import sha256_file
from boardmodeler.simulation.log import LogSummary, parse_log
from boardmodeler.simulation.raw import RawFormatError, read_raw

__all__ = [
    "BATCH_RESOLUTION_NOTES",
    "BatchResult",
    "LocateOutcome",
    "LtspiceInstall",
    "LtspiceLockTimeout",
    "SmokeResult",
    "default_lib_dir",
    "discover",
    "locate",
    "locate_outcome",
    "netlist_step",
    "run_batch",
    "smoke_test",
    "version",
]

SmokeStatus = Literal["pass", "fail"]

_LOG_MARKER = "Total elapsed time"
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")
_LOCK_FILENAME = "boardmodeler-ltspice.lock"


class LtspiceLockTimeout(RuntimeError):
    """Another live process held the simulator lock for the whole timeout.

    Raised instead of running anyway: LTspice hands a second invocation to the
    already-running instance, so two unlocked batch runs silently steal each
    other's deck. Waiting and failing loudly is the only honest option.
    """


def _lock_path() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / _LOCK_FILENAME


def _acquire_lock(timeout_s: float) -> Path:
    """Serialise simulator invocations across processes.

    LTspice behaves as a single-instance application: a second invocation started
    while one is running is handed off to the running instance (observed), so two
    concurrent batch runs can silently steal each other's deck.

    The lock is an **OS-held** exclusive lock on a persistent file in the temp
    directory. That matters: a lock released by a ``finally`` is not released at
    all when the process is killed, and a leftover file then blocks every later
    run while nothing holds it (observed: a dead run's lock stalled three
    unrelated jobs for their full timeout, which then ran unlocked and could
    steal a deck). The operating system drops an OS-held lock when its owner
    exits, so there is no stale state to detect and no mtime heuristic to get
    wrong. The file itself is never deleted, so the lock cannot be lost to a
    delete/acquire race either.

    Raises ``LtspiceLockTimeout`` when a *live* holder keeps the lock for longer
    than ``timeout_s``.
    """
    path = _lock_path()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_RDWR)
        except OSError as exc:  # pragma: no cover - no temp dir write access
            raise LtspiceLockTimeout(f"cannot open the simulator lock file {path}: {exc}") from exc
        try:
            _lock_file_exclusive(handle)
        except OSError:
            os.close(handle)
            if time.monotonic() >= deadline:
                raise LtspiceLockTimeout(
                    f"another BoardModeler process held the simulator lock {path} for "
                    f"{timeout_s:g}s; re-run once it finishes"
                ) from None
            time.sleep(0.05)
            continue
        try:
            os.truncate(handle, 0)
            os.write(handle, f"{os.getpid()}\n".encode())
        except OSError:  # pragma: no cover - the pid note is informational only
            pass
        _OPEN_LOCKS[path] = handle
        return path


#: The lock is held open for the lifetime of the owning run, so the OS keeps it
#: until the process exits (including on a kill) and drops it on any crash.
_OPEN_LOCKS: dict[Path, int] = {}


def _lock_file_exclusive(handle: int) -> None:
    """Take a non-blocking exclusive lock on an open file descriptor."""
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(path: Path | None) -> None:
    if path is None:
        return
    handle = _OPEN_LOCKS.pop(path, None)
    if handle is None:
        return
    with contextlib.suppress(OSError):  # pragma: no cover - best-effort release
        if os.name == "nt":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    with contextlib.suppress(OSError):  # pragma: no cover - closing releases it anyway
        os.close(handle)


BATCH_RESOLUTION_NOTES = (
    "argv = [LTspice.exe, -b, (extra switches...), <absolute deck path>] with cwd=deck dir; "
    "-I is unsupported (GUI modal hang), .step is unsupported (concatenated raw)"
)

#: Explicit override for automation. On its own it is a setting, never a search.
_CANDIDATE_ENV = "LTSPICE_EXE"


@dataclass(frozen=True)
class LtspiceInstall:
    """A located LTspice executable and how it was found."""

    path: Path
    source: str

    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class BatchResult:
    """Observed outcome of one LTspice batch invocation."""

    deck: Path
    run_dir: Path
    exit_code: int
    stdout: str
    stderr: str
    wall_s: float
    timed_out: bool
    raw_path: Path | None = None
    log_path: Path | None = None
    op_raw_path: Path | None = None
    net_path: Path | None = None
    terminated_after_marker: bool = False
    cancelled: bool = False
    argv: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only when the process exited cleanly and produced a log."""
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and self.log_path is not None
        )

    def observed(self) -> str:
        """One-line description of what was actually observed."""
        bits = [
            f"exit_code={self.exit_code}",
            f"wall={self.wall_s:.2f}s",
            f"raw={'yes' if self.raw_path else 'no'}",
            f"log={'yes' if self.log_path else 'no'}",
        ]
        if self.timed_out:
            bits.append("TIMED_OUT")
        if self.cancelled:
            bits.append("CANCELLED")
        if self.terminated_after_marker:
            bits.append("terminated_after_log_marker")
        if self.stderr.strip():
            bits.append(f"stderr={self.stderr.strip()[:200]!r}")
        return "; ".join(bits)


@dataclass(frozen=True)
class SmokeResult:
    """Result of the end-to-end simulator smoke test."""

    status: SmokeStatus
    detail: str
    measured_v: float | None
    expected_v: float
    tolerance_pct: float
    exit_code: int | None = None
    wall_s: float = 0.0
    raw_sha256: str | None = None
    log_sha256: str | None = None
    log_meas_v: float | None = None
    reader_layout: str | None = None
    header_encoding: str | None = None
    version: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "smoke_test": self.status,
            "smoke_detail": self.detail,
            "measured_v": self.measured_v,
            "expected_v": self.expected_v,
            "tolerance_pct": self.tolerance_pct,
            "exit_code": self.exit_code,
            "wall_s": self.wall_s,
            "raw_sha256": self.raw_sha256,
            "log_sha256": self.log_sha256,
            "log_meas_v": self.log_meas_v,
            "reader_layout": self.reader_layout,
            "header_encoding": self.header_encoding,
        }


def default_lib_dir() -> Path | None:
    """The installation's read-only library directory, if it can be found."""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "LTspice" / "lib",
        Path(os.environ.get("APPDATA", "")) / "LTspice" / "lib",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _install_candidates() -> list[tuple[Path, str]]:
    """Well-known install locations, in probe order, for :func:`discover` only.

    Nothing else may call this: an unconfigured machine reports ``unset`` instead of
    inspecting the user's profile, so SETUP has to ask the user exactly once.
    """
    candidates: list[tuple[Path, str]] = []
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(
            (Path(localappdata) / "Programs" / "ADI" / "LTspice" / "LTspice.exe", "LOCALAPPDATA")
        )
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append((Path(program_files) / "ADI" / "LTspice" / "LTspice.exe", "ProgramFiles"))
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if program_files_x86:
        candidates.append(
            (Path(program_files_x86) / "LTC" / "LTspiceXVII" / "XVIIx64.exe", "ProgramFiles(x86)")
        )
        candidates.append(
            (Path(program_files_x86) / "LTC" / "LTspiceIV" / "scad3.exe", "ProgramFiles(x86)")
        )
    return candidates


LocateReason = Literal[
    "configured",
    "config",
    "env",
    "unset",
    "discovered",
    "configured_missing",
    "config_missing",
    "env_missing",
    "not_installed",
]


@dataclass(frozen=True)
class LocateOutcome:
    """What was resolved, which setting it came from, and why."""

    install: LtspiceInstall | None
    probed: list[tuple[Path, str]]
    reason: LocateReason

    @property
    def probed_paths(self) -> list[str]:
        return [str(path) for path, _source in self.probed]


def _resolve_one(
    path: Path, source: str, *, found: LocateReason, missing: LocateReason
) -> LocateOutcome:
    """Report exactly one configured path: used when it is a file, missing otherwise."""
    install = LtspiceInstall(path=path, source=source) if path.is_file() else None
    return LocateOutcome(
        install=install, probed=[(path, source)], reason=found if install else missing
    )


def _configured_path() -> str | None:
    """The executable path saved by SETUP, or ``None`` when it was never set."""
    from boardmodeler.config import load_config

    return load_config().ltspice.path


def locate_outcome(explicit: str | Path | None = None) -> LocateOutcome:
    """Resolve LTspice from what the user configured, never by searching the machine.

    The order is the ``explicit`` argument, then the saved ``ltspice.path`` from the
    config file, then ``LTSPICE_EXE``. A configured entry that is missing is reported
    missing — it does **not** fall through to another installation, because silently
    simulating with a different binary than the one that was asked for would
    invalidate every result. With none of the three set, the outcome is ``unset``:
    SETUP has not been completed, and no install location is touched. Use
    :func:`discover` when the user explicitly asks for a search.
    """
    if explicit:
        return _resolve_one(
            Path(explicit),
            "configured",
            found="configured",
            missing="configured_missing",
        )
    configured = _configured_path()
    if configured:
        return _resolve_one(Path(configured), "config", found="config", missing="config_missing")
    env_value = os.environ.get(_CANDIDATE_ENV)
    if env_value:
        return _resolve_one(
            Path(env_value),
            f"env:{_CANDIDATE_ENV}",
            found="env",
            missing="env_missing",
        )
    return LocateOutcome(install=None, probed=[], reason="unset")


def locate(explicit: str | Path | None = None) -> LtspiceInstall | None:
    """The configured LTspice executable, or ``None`` when SETUP has not set one.

    Never searches: an unconfigured machine reports nothing until the user chooses
    an executable in SETUP, or exports ``LTSPICE_EXE`` for automation.
    """
    return locate_outcome(explicit).install


def discover(explicit: str | Path | None = None) -> LocateOutcome:
    """Probe well-known install locations because the user explicitly asked.

    This is the SETUP page's find button and the explicit CLI search. It never runs
    as part of :func:`locate`, and never at startup; the probed list is returned so
    the caller can show exactly which locations were inspected.
    """
    candidates = _install_candidates()
    if explicit:
        candidates.insert(0, (Path(explicit), "configured"))
    env_value = os.environ.get(_CANDIDATE_ENV)
    if env_value:
        candidates.insert(0 if not explicit else 1, (Path(env_value), f"env:{_CANDIDATE_ENV}"))
    for path, source in candidates:
        if not path.is_file():
            continue
        if source == "configured":
            reason: LocateReason = "configured"
        elif source.startswith("env:"):
            reason = "env"
        else:
            reason = "discovered"
        return LocateOutcome(
            install=LtspiceInstall(path=path, source=source), probed=candidates, reason=reason
        )
    return LocateOutcome(install=None, probed=candidates, reason="not_installed")


def version(exe: Path, *, timeout_s: float = 20.0) -> str | None:
    """Version reported by ``LTspice.exe -version`` (e.g. ``26.0.0``)."""
    try:
        proc = subprocess.run(
            [str(exe), "-version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    match = _VERSION_RE.search(proc.stdout or "")
    return match.group(1) if match else None


def _kill_tree(pid: int) -> None:
    """Kill the process tree rooted at ``pid`` (which we spawned ourselves)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:  # pragma: no cover - LTspice is Windows-only
        subprocess.run(["pkill", "-P", str(pid)], capture_output=True, check=False)


def _purge_outputs(outputs: dict[str, Path]) -> list[Path]:
    """Remove stale artifacts of the same stem before launching a run."""
    removed: list[Path] = []
    for path in outputs.values():
        if path.is_file():
            try:
                path.unlink()
                removed.append(path)
            except OSError:  # pragma: no cover - a locked file is reported by the run itself
                continue
    for extra in outputs["raw"].parent.glob(f"{outputs['raw'].stem}*.db"):
        try:
            extra.unlink()
            removed.append(extra)
        except OSError:  # pragma: no cover
            continue
    return removed


def _output_paths(deck: Path, run_dir: Path) -> dict[str, Path]:
    stem = deck.stem
    return {
        "raw": run_dir / f"{stem}.raw",
        "log": run_dir / f"{stem}.log",
        "op_raw": run_dir / f"{stem}.op.raw",
        "net": run_dir / f"{stem}.net",
    }


def _log_has_marker(log_path: Path) -> bool:
    try:
        data = log_path.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    text = (
        data.decode("utf-16-le", errors="ignore")
        if data[1:2] == b"\x00"
        else data.decode("utf-8", errors="ignore")
    )
    return _LOG_MARKER in text


def run_batch(
    exe: Path,
    deck: Path,
    run_dir: Path,
    *,
    timeout_s: float,
    extra_switches: Sequence[str] = (),
    ascii_raw: bool = False,
    marker_grace_s: float = 5.0,
    poll_s: float = 0.1,
    ltspice_lib_dir: Path | None = None,
    cancel: threading.Event | None = None,
    lock_timeout_s: float = 900.0,
) -> BatchResult:
    """Run ``LTspice.exe -b`` on ``deck`` and report exactly what happened.

    ``run_dir`` is the working directory. LTspice writes its outputs beside the
    deck, so when ``deck.parent != run_dir`` the outputs are looked up next to
    the deck (reported through the returned paths).

    ``search_paths``/``-I`` is deliberately absent: see the module docstring.
    """
    deck = Path(deck).resolve()
    run_dir = Path(run_dir).resolve()
    if not deck.is_file():
        raise FileNotFoundError(f"deck not found: {deck}")
    run_dir.mkdir(parents=True, exist_ok=True)
    if ltspice_lib_dir is not None:
        _reject_inside_install(deck, run_dir, Path(ltspice_lib_dir))
    if not exe.is_file():
        raise FileNotFoundError(f"LTspice executable not found: {exe}")

    switches = ["-b"]
    if ascii_raw:
        switches.append("-ascii")
    switches.extend(str(s) for s in extra_switches)
    argv = [str(exe), *switches, str(deck)]

    outputs = _output_paths(deck, deck.parent)
    lock = _acquire_lock(lock_timeout_s)
    try:
        return _run_locked(
            exe=exe,
            deck=deck,
            run_dir=run_dir,
            outputs=outputs,
            argv=argv,
            timeout_s=timeout_s,
            marker_grace_s=marker_grace_s,
            poll_s=poll_s,
            cancel=cancel,
            lock_acquired=True,
        )
    finally:
        _release_lock(lock)


def _native_path(path: Path) -> str:
    """Keep long evidence paths while giving legacy Windows tools an existing short alias."""
    text = str(path)
    if os.name != "nt" or len(text) < 240:
        return text
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    is_file = path.is_file()
    target = str(path.parent) if is_file else text
    count = ctypes.windll.kernel32.GetShortPathNameW(target, buffer, len(buffer))
    native = str(Path(buffer.value) / path.name) if is_file else buffer.value
    if count and count < len(buffer) and len(native) < 240:
        # Preserve the deck basename: LTspice derives .raw/.log names from
        # argv, so an 8.3 alias for the file itself would hide its outputs.
        return native
    raise OSError(
        "windows_path_too_long: this volume has no short path alias; choose a shorter model folder"
    )


def _run_locked(
    *,
    exe: Path,
    deck: Path,
    run_dir: Path,
    outputs: dict[str, Path],
    argv: list[str],
    timeout_s: float,
    marker_grace_s: float,
    poll_s: float,
    cancel: threading.Event | None,
    lock_acquired: bool,
) -> BatchResult:
    # A run's artifacts must belong to that run: a stale log from an earlier run
    # in the same directory contains "Total elapsed time" and would make the
    # watchdog below kill a fresh run after its grace period.
    _purge_outputs(outputs)

    started = time.monotonic()
    proc = subprocess.Popen(
        [*argv[:-1], _native_path(deck)],
        cwd=_native_path(run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    outputs = _output_paths(deck, deck.parent)
    deadline = started + timeout_s
    grace_deadline: float | None = None
    timed_out = False
    cancelled = False
    terminated_after_marker = False
    stdout = stderr = ""

    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if cancel is not None and cancel.is_set():
                cancelled = True
                _kill_tree(proc.pid)
                stdout, stderr = _drain(proc)
                break
            if grace_deadline is None and _log_has_marker(outputs["log"]):
                grace_deadline = now + marker_grace_s
            if grace_deadline is not None and now >= grace_deadline:
                terminated_after_marker = True
                _kill_tree(proc.pid)
                stdout, stderr = _drain(proc)
                break
            if now >= deadline:
                timed_out = True
                _kill_tree(proc.pid)
                stdout, stderr = _drain(proc)
                break

    wall_s = time.monotonic() - started
    return BatchResult(
        deck=deck,
        run_dir=run_dir,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        wall_s=wall_s,
        timed_out=timed_out,
        terminated_after_marker=terminated_after_marker,
        cancelled=cancelled,
        raw_path=outputs["raw"] if outputs["raw"].is_file() else None,
        log_path=outputs["log"] if outputs["log"].is_file() else None,
        op_raw_path=outputs["op_raw"] if outputs["op_raw"].is_file() else None,
        net_path=outputs["net"] if outputs["net"].is_file() else None,
        argv=tuple(argv),
        extra={"lock_acquired": str(lock_acquired)},
    )


def _drain(proc: subprocess.Popen) -> tuple[str, str]:
    try:
        out, err = proc.communicate(timeout=10)
        return out or "", err or ""
    except subprocess.TimeoutExpired, ValueError, OSError:  # pragma: no cover
        return "", ""


def _reject_inside_install(deck: Path, run_dir: Path, lib_dir: Path) -> None:
    for target in (deck, run_dir):
        try:
            target.relative_to(lib_dir)
        except ValueError:
            continue
        raise ValueError(
            f"refusing to write into the LTspice installation/library directory: {target} "
            f"is inside {lib_dir}"
        )


def netlist_step(
    exe: Path,
    schematic: Path,
    *,
    timeout_s: float = 60.0,
    run_dir: Path | None = None,
) -> BatchResult:
    """Run ``LTspice.exe -netlist <schematic>`` and report the produced ``.net``.

    Symbols must be resolvable without ``-I``: the schematic's own directory is
    searched first (verified), so generated ``.asy`` files are placed beside the
    schematic.
    """
    schematic = Path(schematic).resolve()
    work = Path(run_dir).resolve() if run_dir else schematic.parent
    work.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    proc = subprocess.Popen(
        [str(exe), "-netlist", str(schematic)],
        cwd=str(work),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    terminated_after_marker = False
    deadline = started + timeout_s
    marker_log = schematic.with_suffix(".log")
    grace_deadline: float | None = None
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if grace_deadline is None and _log_has_marker(marker_log):
                grace_deadline = now + 5.0
            if grace_deadline is not None and now >= grace_deadline:
                terminated_after_marker = True
                _kill_tree(proc.pid)
                stdout, stderr = _drain(proc)
                break
            if now >= deadline:
                timed_out = True
                _kill_tree(proc.pid)
                stdout, stderr = _drain(proc)
                break

    outputs = _output_paths(schematic, schematic.parent)
    return BatchResult(
        deck=schematic,
        run_dir=work,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        wall_s=time.monotonic() - started,
        timed_out=timed_out,
        terminated_after_marker=terminated_after_marker,
        net_path=outputs["net"] if outputs["net"].is_file() else None,
        log_path=outputs["log"] if outputs["log"].is_file() else None,
        argv=(str(exe), "-netlist", str(schematic)),
    )


# --------------------------------------------------------------------------- #
# smoke test


SMOKE_DECK = """* BoardModeler LTspice smoke test: 1 V step into a 1 k / 1 uF RC
V1 in 0 PULSE(0 1 0 1n 1n 1 2)
R1 in out 1k
C1 out 0 1u
.tran 10m
.meas TRAN vout_at_1ms FIND V(out) AT=1m
.end
"""

SMOKE_CIRCUIT = "smoke_rc.cir"
SMOKE_EXPECTED_V = 0.632
SMOKE_TOLERANCE_PCT = 2.0
SMOKE_MEAS_NAME = "vout_at_1ms"


def smoke_test(
    exe: Path,
    workdir: Path,
    *,
    timeout_s: float = 60.0,
) -> SmokeResult:
    """End-to-end simulator check against a closed-form RC step response.

    Requires, in order: the process exits 0, ``.raw`` and ``.log`` exist, the log
    shows ``Total elapsed time``, the independent NumPy read of ``.raw`` yields
    0.632 V +/- 2 % at 1 ms, and the ``.meas`` value from the log agrees with the
    waveform read to within 1 %. Every failure path returns ``fail`` with the
    observed values in ``detail``.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    deck = workdir / SMOKE_CIRCUIT
    deck.write_text(SMOKE_DECK, encoding="utf-8")
    exe = Path(exe)
    if not exe.is_file():
        return SmokeResult(
            status="fail",
            detail=f"LTspice executable not found: {exe}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
        )
    exe_version = version(exe)

    try:
        result = run_batch(exe, deck, workdir, timeout_s=timeout_s)
    except LtspiceLockTimeout as exc:
        return SmokeResult(
            status="fail",
            detail=f"the simulator was busy: {exc}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            version=exe_version,
        )
    observed = result.observed()

    if result.timed_out:
        return SmokeResult(
            status="fail",
            detail=f"LTspice did not finish within {timeout_s:.0f}s ({observed})",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            version=exe_version,
        )
    if result.exit_code != 0:
        log_tail = _tail(result.log_path)
        return SmokeResult(
            status="fail",
            detail=f"LTspice exited with code {result.exit_code} ({observed}); log tail: {log_tail}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            version=exe_version,
        )
    if result.raw_path is None or result.log_path is None:
        missing = "raw" if result.raw_path is None else "log"
        return SmokeResult(
            status="fail",
            detail=f"LTspice exited 0 but the {missing} file is missing ({observed})",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            version=exe_version,
        )

    summary: LogSummary = parse_log(result.log_path)
    raw_sha = sha256_file(result.raw_path)
    log_sha = sha256_file(result.log_path)

    if not summary.completed:
        return SmokeResult(
            status="fail",
            detail=(
                f"log has no '{_LOG_MARKER}' line, so the run did not complete; "
                f"{summary.summary_line()}"
            ),
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            raw_sha256=raw_sha,
            log_sha256=log_sha,
            version=exe_version,
        )

    try:
        raw = read_raw(result.raw_path)
    except RawFormatError as exc:
        return SmokeResult(
            status="fail",
            detail=f"cannot read {result.raw_path.name}: {exc}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            raw_sha256=raw_sha,
            log_sha256=log_sha,
            version=exe_version,
        )

    if not raw.has("V(out)"):
        return SmokeResult(
            status="fail",
            detail=f"V(out) is not in the .raw variables: {raw.variables}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            raw_sha256=raw_sha,
            log_sha256=log_sha,
            reader_layout=raw.layout,
            header_encoding=raw.header_encoding,
            version=exe_version,
        )

    time_axis = raw.time_column()
    if time_axis is None:
        return SmokeResult(
            status="fail",
            detail=f"no time axis in the .raw variables: {raw.variables}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            raw_sha256=raw_sha,
            log_sha256=log_sha,
            reader_layout=raw.layout,
            header_encoding=raw.header_encoding,
            version=exe_version,
        )

    try:
        measured = float(np.interp(1e-3, time_axis, raw.column("V(out)")))
    except (ValueError, IndexError) as exc:
        # Documented contract: a payload that cannot be read is a fail, not a crash.
        return SmokeResult(
            status="fail",
            detail=f"cannot interpolate V(out) at 1 ms: {exc}",
            measured_v=None,
            expected_v=SMOKE_EXPECTED_V,
            tolerance_pct=SMOKE_TOLERANCE_PCT,
            exit_code=result.exit_code,
            wall_s=result.wall_s,
            raw_sha256=raw_sha,
            log_sha256=log_sha,
            reader_layout=raw.layout,
            header_encoding=raw.header_encoding,
            version=exe_version,
        )
    deviation_pct = abs(measured - SMOKE_EXPECTED_V) / SMOKE_EXPECTED_V * 100.0
    log_meas = summary.value(SMOKE_MEAS_NAME)

    agreement = ""
    status: SmokeStatus = "pass"
    if log_meas is not None:
        delta_pct = abs(log_meas - measured) / SMOKE_EXPECTED_V * 100.0
        agreement = f"; .meas={log_meas:.6f} V (delta {delta_pct:.4f}%)"
        if delta_pct > 1.0:
            status = "fail"

    if deviation_pct > SMOKE_TOLERANCE_PCT:
        status = "fail"
    if abs(measured) < 1e-9:
        # A zero-filled or unread payload must never masquerade as a pass.
        status = "fail"

    detail = (
        f"V(out)@{1e-3:.0e}s = {measured:.6f} V (analytic {SMOKE_EXPECTED_V:.3f} V, "
        f"deviation {deviation_pct:.3f}%, tolerance +/-{SMOKE_TOLERANCE_PCT}%)"
        f"{agreement}; layout={raw.layout}; encoding={raw.header_encoding}; "
        f"raw sha256={raw_sha[:16]}...; log sha256={log_sha[:16]}...; {observed}"
    )
    return SmokeResult(
        status=status,
        detail=detail,
        measured_v=measured,
        expected_v=SMOKE_EXPECTED_V,
        tolerance_pct=SMOKE_TOLERANCE_PCT,
        exit_code=result.exit_code,
        wall_s=result.wall_s,
        raw_sha256=raw_sha,
        log_sha256=log_sha,
        log_meas_v=log_meas,
        reader_layout=raw.layout,
        header_encoding=raw.header_encoding,
        version=exe_version,
    )


def _tail(path: Path | None, limit: int = 300) -> str:
    if path is None or not path.is_file():
        return "(no log)"
    data = path.read_bytes()
    text = (
        data.decode("utf-16-le", errors="replace")
        if data[1:2] == b"\x00"
        else data.decode("utf-8", errors="replace")
    )
    return text.strip()[-limit:]
