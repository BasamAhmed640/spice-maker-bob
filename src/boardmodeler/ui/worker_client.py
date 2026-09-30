"""Qt client for the worker protocol (INTERFACES §2/§4).

``WorkerClient`` spawns ``python -m boardmodeler.pipeline.worker`` with a list
argv (``shell=False``) and re-emits every protocol event as a Qt signal. The GUI
never computes a verdict: it renders the events this client forwards.

Signals carry the decoded event dictionary unchanged; ``exited(int)`` fires once
the child is gone and ``outcome`` then holds everything the child said (exit
code, terminal ``result``/``error`` events, stderr, protocol violations).
"""

from __future__ import annotations

import enum
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

__all__ = [
    "EVENT_SIGNALS",
    "WorkerClient",
    "WorkerOutcome",
    "jsonable_request",
    "terminate_process_tree",
]

EVENT_SIGNALS: dict[str, str] = {
    "stage": "stage",
    "progress": "progress",
    "findings": "findings",
    "review": "review",
    "waveform": "waveform",
    "result": "result",
    "error": "error",
}
"""Protocol event kind -> ``WorkerClient`` signal name."""

_TERMINAL_EVENTS = ("result", "error")


@dataclass
class WorkerOutcome:
    """Everything observed about one worker run."""

    exit_code: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    malformed_lines: list[str] = field(default_factory=list)
    stderr: str = ""
    request_path: Path | None = None

    @property
    def ok(self) -> bool:
        """True when the worker exited 0 (statuses inside are data, not success)."""
        return self.exit_code == 0

    @property
    def status(self) -> str | None:
        return str(self.result["status"]) if self.result and "status" in self.result else None

    @property
    def summary(self) -> dict[str, int]:
        if not self.result:
            return {}
        raw = self.result.get("summary", {})
        return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    @property
    def results(self) -> list[dict[str, Any]]:
        if not self.result:
            return []
        raw = self.result.get("results", [])
        return [dict(item) for item in raw] if isinstance(raw, list) else []

    @property
    def completed(self) -> bool:
        return self.exit_code == 0 and (self.result is not None or self.error is not None)


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    enum_value = getattr(value, "value", None)
    if enum_value is not None and not isinstance(value, Mapping | list | tuple | set):
        return _jsonable(enum_value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    return str(value)


def jsonable_request(request: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Normalise a request object (dataclass/pydantic/mapping) to JSON types."""
    if isinstance(request, Mapping):
        return {str(k): _jsonable(v) for k, v in request.items()}
    return dict(_jsonable(request))


def terminate_process_tree(pid: int, *, timeout_s: float = 5.0) -> list[int]:
    """Terminate ``pid`` and every descendant; returns the PIDs targeted.

    Uses ``psutil`` when it is importable (deterministic in tests) and falls
    back to ``taskkill /T`` on Windows / process-group kill elsewhere.
    """
    if pid <= 0:
        return []
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a dev/optional dependency
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            parent = psutil.Process(pid)
        except psutil.Error:
            return []
        targets = []
        try:
            targets = parent.children(recursive=True)
        except psutil.Error:
            targets = []
        all_targets = [*targets, parent]
        for proc in all_targets:
            try:
                proc.terminate()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(all_targets, timeout=timeout_s)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:  # pragma: no cover - process already gone
                continue
        return [proc.pid for proc in all_targets]

    if os.name == "nt":  # pragma: no cover - exercised only without psutil
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return [pid]
    try:  # pragma: no cover - LTspice/BoardModeler target Windows
        os.killpg(os.getpgid(pid), 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            return []
    return [pid]


class WorkerClient(QObject):
    """Runs one worker child process at a time and forwards its events."""

    stage = Signal(dict)
    progress = Signal(dict)
    findings = Signal(dict)
    review = Signal(dict)
    waveform = Signal(dict)
    result = Signal(dict)
    error = Signal(dict)
    resumed = Signal(dict)
    started = Signal(dict)
    stderr_text = Signal(str)
    exited = Signal(int)

    def __init__(
        self,
        *,
        python: Path | str | None = None,
        project_dir: Path | str | None = None,
        cancel_timeout_s: float = 5.0,
        controller_module: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = Path(python) if python is not None else Path(sys.executable)
        self._project_dir = Path(project_dir) if project_dir is not None else None
        self._cancel_timeout_s = float(cancel_timeout_s)
        self._controller_module = controller_module
        self._proc: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._outcome = WorkerOutcome()
        self._last_request: dict[str, Any] | None = None
        self._last_project: Path | None = None
        self._workdir: Path | None = None
        self._finished = threading.Event()

    # ------------------------------------------------------------------ state

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def outcome(self) -> WorkerOutcome:
        """Observations so far; complete once :attr:`exited` has fired."""
        with self._lock:
            return self._outcome

    @property
    def last_request(self) -> dict[str, Any] | None:
        return dict(self._last_request) if self._last_request is not None else None

    @property
    def request_path(self) -> Path | None:
        return self._outcome.request_path

    # ------------------------------------------------------------------ start

    def start(
        self,
        request: Mapping[str, Any] | Any,
        *,
        project_dir: Path | str | None = None,
    ) -> Path:
        """Spawn the worker for ``request``; returns the written request path."""
        if self.is_running():
            raise RuntimeError("a worker run is already in progress")

        payload = jsonable_request(request)
        project = Path(project_dir) if project_dir is not None else self._project_dir
        if project is None:
            raw = payload.get("project_dir")
            project = Path(str(raw)) if raw else Path.cwd()

        workdir = Path(tempfile.mkdtemp(prefix="boardmodeler-ui-"))
        request_path = workdir / f"request-{uuid.uuid4().hex[:8]}.json"
        request_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        self._last_request = payload
        self._last_project = project
        self._workdir = workdir
        with self._lock:
            self._outcome = WorkerOutcome(request_path=request_path)
        self._finished.clear()

        argv = [
            str(self._python),
            "-m",
            "boardmodeler.pipeline.worker",
            "--request",
            str(request_path),
            "--project",
            str(project),
        ]
        if self._controller_module:
            argv += ["--controller-module", str(self._controller_module)]
        self._proc = subprocess.Popen(
            argv,
            cwd=str(project),
            env={key: value for key, value in os.environ.items() if key.upper() != "LTSPICE_EXE"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        self.started.emit(
            {"argv": argv, "request_path": str(request_path), "project": str(project)}
        )

        self._threads = [
            threading.Thread(target=self._pump_stdout, name="worker-stdout", daemon=True),
            threading.Thread(target=self._pump_stderr, name="worker-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return request_path

    def resume(self) -> bool:
        """Re-run the last request (the protocol allows resuming by re-running)."""
        if self.is_running() or self._last_request is None:
            return False
        payload = dict(self._last_request)
        self.resumed.emit(payload)
        self.start(payload, project_dir=self._last_project)
        return True

    # ------------------------------------------------------------------ cancel

    def cancel(self) -> bool:
        """Kill the worker's whole process tree; ``False`` when nothing runs."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        with self._lock:
            self._outcome.cancelled = True
        terminate_process_tree(proc.pid, timeout_s=self._cancel_timeout_s)
        return True

    def wait(self, timeout_s: float | None = None) -> WorkerOutcome:
        """Block until the worker has exited (tests and shutdown paths)."""
        self._finished.wait(timeout_s)
        for thread in self._threads:
            thread.join(timeout=1.0)
        return self.outcome

    # ------------------------------------------------------------------ pumps

    def _pump_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                with self._lock:
                    self._outcome.malformed_lines.append(text)
                self.stderr_text.emit(f"protocol violation (not JSON): {text}")
                continue
            if not isinstance(event, dict) or "event" not in event:
                with self._lock:
                    self._outcome.malformed_lines.append(text)
                self.stderr_text.emit(f"protocol violation (no event field): {text}")
                continue
            self._dispatch(event)
        code = proc.wait()
        self._finalise(code)

    def _pump_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            text = line.rstrip("\n")
            with self._lock:
                self._outcome.stderr = (
                    (self._outcome.stderr + text + "\n") if text else self._outcome.stderr
                )
            if text:
                self.stderr_text.emit(text)

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event", ""))
        with self._lock:
            self._outcome.events.append(event)
            if kind == "stage":
                self._outcome.stages.append(event)
            elif kind == "result":
                self._outcome.result = event
            elif kind == "error":
                self._outcome.error = event
        signal = getattr(self, EVENT_SIGNALS.get(kind, ""), None)
        if signal is not None:
            signal.emit(event)
        else:
            self.stderr_text.emit(f"protocol violation (unknown event kind {kind!r})")

    def _finalise(self, exit_code: int) -> None:
        with self._lock:
            self._outcome.exit_code = exit_code
            cancelled = self._outcome.cancelled or (
                self._outcome.error is not None
                and str(self._outcome.error.get("code")) == "cancelled"
            )
            self._outcome.cancelled = cancelled
        self._finished.set()
        self.exited.emit(int(exit_code))
