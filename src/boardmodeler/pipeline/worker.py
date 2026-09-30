"""Worker protocol (INTERFACES §2).

Invocation::

    python -m boardmodeler.pipeline.worker --request <request.json> --project <dir>

``stdout`` carries **one JSON object per line** and nothing else; diagnostics go
to ``stderr``. The worker exits 0 for any completed run (statuses are data) and
non-zero only when the request itself could not be served:

======  ==========================================================
exit    meaning
======  ==========================================================
0       the run completed (whatever statuses it produced)
1       internal error / repair violation raised out of the controller
2       the request could not be served (bad JSON, unknown fields,
        project missing, controller module unavailable)
130     cancelled via SIGINT/SIGTERM/SIGBREAK
======  ==========================================================

Cancellation is honoured on a best-effort basis: on Windows a ``SIGBREAK``/
``SIGTERM`` handler runs only when the main thread next checks for signals, so a
controller blocked in a long uninterruptible call exits late. The GUI never
relies on this — it terminates the worker's process tree
(``ui.worker_client.WorkerClient.cancel``).

The pipeline controller is imported **lazily**, inside :func:`main`, so every
module that only wants the protocol (and its tests) works before
``pipeline/controller.py`` exists on disk. ``--controller-module`` selects the
module to import and defaults to :data:`CONTROLLER_MODULE`; this is also the
seam the protocol tests use to drive the worker end to end without a real
pipeline run.

``--list-events`` is a self-check mode: it emits the documented event shapes
from a synthetic result without touching the pipeline or the simulator, so the
protocol framing can be verified on any machine.
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import importlib
import json
import signal
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import Finding, ReviewItem, TestResult

__all__ = [
    "CONTROLLER_MODULE",
    "EVENT_KINDS",
    "EXIT_CANCELLED",
    "EXIT_INTERNAL_ERROR",
    "EXIT_OK",
    "EXIT_REQUEST_ERROR",
    "EventWriter",
    "RequestError",
    "build_request",
    "load_request_payload",
    "main",
    "make_cancel_handler",
    "render_event",
    "run_request",
    "synthetic_events",
]

CONTROLLER_MODULE = "boardmodeler.pipeline.controller"
"""Default module the worker imports lazily for ``PipelineController``."""

EVENT_KINDS: tuple[str, ...] = (
    "stage",
    "progress",
    "findings",
    "review",
    "waveform",
    "result",
    "error",
)

EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_REQUEST_ERROR = 2
EXIT_CANCELLED = 130

_MAX_SIGNAL_RAW_BYTES = 32 * 1024 * 1024
"""A ``.raw`` larger than this is not read just to list its signals."""


class RequestError(RuntimeError):
    """The request itself could not be served; ``code`` is a protocol code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# encoding


def render_event(event: Mapping[str, Any]) -> str:
    """Canonical single-line JSON for one protocol event (no trailing newline)."""
    return json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))


class EventWriter:
    """Writes protocol events, one JSON object per line, to a text stream."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: Mapping[str, Any]) -> None:
        line = render_event(event)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def _jsonable(value: Any) -> Any:
    """Convert records/Path/enums into JSON-compatible values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclass_fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, Iterable):
        return [_jsonable(v) for v in value]
    return str(value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or object without requiring a class."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    return str(enum_value) if enum_value is not None else str(value)


# --------------------------------------------------------------------------- #
# event shapes


def stage_event(progress: Any) -> dict[str, Any]:
    """``{"event": "stage", ...}`` for one :class:`StageProgress`."""
    counts = _field(progress, "test_counts", {}) or {}
    return {
        "event": "stage",
        "stage": _text(_field(progress, "stage")),
        "status": _text(_field(progress, "status")),
        "detail": _text(_field(progress, "detail")),
        "elapsed_s": float(_field(progress, "elapsed_s", 0.0) or 0.0),
        "test_counts": {str(k): int(v) for k, v in dict(counts).items()},
        "artifacts": [str(a) for a in (_field(progress, "artifacts", []) or [])],
    }


def progress_event(stage: Any, done: int, total: int, detail: str = "") -> dict[str, Any]:
    """``{"event": "progress", ...}`` — position of the stage chain."""
    return {
        "event": "progress",
        "stage": _text(stage),
        "done": int(done),
        "total": int(total),
        "detail": _text(detail),
    }


def findings_event(findings: Sequence[Any]) -> dict[str, Any]:
    return {"event": "findings", "findings": [_jsonable(f) for f in findings]}


def review_event(items: Sequence[Any]) -> dict[str, Any]:
    return {"event": "review", "items": [_jsonable(item) for item in items]}


def waveform_event(
    ref: str, signals: Sequence[str], violations: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    return {
        "event": "waveform",
        "ref": ref,
        "signals": [str(s) for s in signals],
        "violations": [dict(v) for v in violations],
    }


def error_event(code: str, detail: str) -> dict[str, Any]:
    return {"event": "error", "code": str(code), "detail": str(detail)}


def result_event(result: Any) -> dict[str, Any]:
    """``{"event": "result", ...}`` — the terminal event of a completed run."""
    results = list(_field(result, "results", []) or [])
    summary: dict[str, int] = {}
    for item in results:
        status = _text(_field(item, "status"))
        summary[status] = summary.get(status, 0) + 1
    manifest_path = _field(result, "manifest_path")
    export_dir = _field(result, "export_dir")
    return {
        "event": "result",
        "status": _text(_field(result, "status")),
        "summary": summary,
        "results": [_jsonable(item) for item in results],
        "findings": [_jsonable(f) for f in (_field(result, "findings", []) or [])],
        "review_items": [_jsonable(i) for i in (_field(result, "review_items", []) or [])],
        "artifacts": [str(a) for a in (_field(result, "artifacts", []) or [])],
        "manifest": str(manifest_path) if manifest_path else None,
        "export_dir": str(export_dir) if export_dir else None,
        "diagnostics": {
            str(k): str(v) for k, v in dict(_field(result, "diagnostics", {}) or {}).items()
        },
        "stages": [_jsonable(s) for s in (_field(result, "stages", []) or [])],
    }


# --------------------------------------------------------------------------- #
# request handling


def load_request_payload(path: Path) -> dict[str, Any]:
    """Read a request JSON object; malformed input is a request error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RequestError("request_unreadable", f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RequestError("request_invalid", f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestError("request_invalid", f"{path} must hold a JSON object")
    return payload


def build_request(
    request_class: type,
    payload: Mapping[str, Any],
    *,
    project_dir: Path | None = None,
) -> Any:
    """Construct the controller's ``PipelineRequest`` from the JSON payload.

    Unknown fields are rejected rather than ignored: a silently dropped
    ``allow_remot`` would change what the run was allowed to do.
    """
    known = {f.name for f in dataclass_fields(request_class)}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise RequestError("request_invalid", f"unknown request field(s): {', '.join(unknown)}")

    kwargs: dict[str, Any] = {}
    for name, value in payload.items():
        if name in ("project_dir", "export_dir"):
            kwargs[name] = Path(str(value)) if value is not None else None
        elif name == "document_paths":
            kwargs[name] = [Path(str(p)) for p in (value or [])]
        elif name == "part_identity" and value is not None:
            from boardmodeler.domain.records import PartIdentity

            kwargs[name] = PartIdentity.model_validate(value)
        else:
            kwargs[name] = value

    effective = project_dir if project_dir is not None else kwargs.get("project_dir")
    if effective is None:
        raise RequestError(
            "request_invalid", "no project directory: pass --project or set project_dir"
        )
    effective = Path(effective)
    if not effective.is_dir():
        raise RequestError("project_not_found", f"project directory does not exist: {effective}")
    kwargs["project_dir"] = effective
    return request_class(**kwargs)


def stage_sequence(module: ModuleType) -> tuple[str, ...]:
    """``STAGE_ORDER`` from the controller, when it declares one."""
    order = getattr(module, "STAGE_ORDER", ())
    return tuple(_text(stage) for stage in order)


# --------------------------------------------------------------------------- #
# running


def _read_signal_names(path: Path) -> list[str]:
    """Variable names from a ``.raw``, or ``[]`` when it cannot be read cheaply."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SIGNAL_RAW_BYTES:
            return []
        from boardmodeler.simulation.raw import read_raw

        return list(read_raw(path).variables)
    except Exception:  # a waveform that cannot be read is not an error here
        return []


def emit_waveforms(writer: EventWriter, result: Any, project_dir: Path) -> None:
    """One ``waveform`` event per distinct ``TestResult.waveform_ref``."""
    seen: set[str] = set()
    for item in _field(result, "results", []) or []:
        for ref in _field(item, "waveform_refs", []) or []:
            ref_text = str(ref)
            if not ref_text or ref_text in seen:
                continue
            seen.add(ref_text)
            writer.emit(waveform_event(ref_text, _read_signal_names(project_dir / ref_text)))


def run_request(
    controller: Any,
    request: Any,
    writer: EventWriter,
    *,
    cancel: threading.Event | None = None,
    stage_order: Sequence[str] = (),
) -> Any:
    """Execute ``request`` and emit the full protocol stream for it."""
    total = len(stage_order)
    seen = 0

    def on_progress(progress: Any) -> None:
        nonlocal seen
        writer.emit(stage_event(progress))
        seen += 1
        if total:
            writer.emit(
                progress_event(
                    _field(progress, "stage"), seen, total, _text(_field(progress, "detail"))
                )
            )

    result = controller.run(request, progress=on_progress, cancel=cancel)

    writer.emit(findings_event(list(_field(result, "findings", []) or [])))
    writer.emit(review_event(list(_field(result, "review_items", []) or [])))
    emit_waveforms(writer, result, Path(str(_field(request, "project_dir"))))
    writer.emit(result_event(result))
    return result


# --------------------------------------------------------------------------- #
# cancellation


def make_cancel_handler(
    writer: EventWriter, cancel: threading.Event, *, exit_code: int = EXIT_CANCELLED
) -> Any:
    """Signal handler that records the cancellation and exits ``exit_code``."""

    def handler(signum: int, frame: Any) -> None:
        del frame  # a signal handler's signature is (signum, frame)
        if not cancel.is_set():
            cancel.set()
            writer.emit(error_event("cancelled", f"received signal {signum}"))
        raise SystemExit(exit_code)

    return handler


def _install_signal_handlers(writer: EventWriter, cancel: threading.Event) -> list[int]:
    installed: list[int] = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, make_cancel_handler(writer, cancel))
        except ValueError, OSError, RuntimeError:  # not the main thread / unsupported
            continue
        installed.append(int(signum))
    return installed


# --------------------------------------------------------------------------- #
# synthetic self-check


def synthetic_events() -> list[dict[str, Any]]:
    """The documented event shapes, built from clearly-labeled synthetic data."""
    results = [
        TestResult(
            test_id="SYNTHETIC_self_check_pass",
            status=Status.PASS,
            measured={"V(out)": 0.632},
            expected="V(out) = 0.632 V at 1 ms",
            detail="synthetic self-check value (not a simulator run)",
        ),
        TestResult(
            test_id="SYNTHETIC_self_check_unknown",
            status=Status.UNKNOWN,
            expected="measurement present",
            detail="synthetic self-check: no measurement was taken",
            unknown_reason="synthetic_self_check",
        ),
        TestResult(
            test_id="SYNTHETIC_self_check_blocked",
            status=Status.BLOCKED,
            expected="simulator available",
            detail="synthetic self-check: simulator not started",
            blocked_reason="synthetic_self_check",
        ),
    ]
    finding = Finding(
        code="SC009_supply_domain_assignment",
        status=Status.FAIL,
        refdes="SYNTHETIC_U1",
        nets=["3V3"],
        message="synthetic self-check finding (no circuit was read)",
        detail={"observed_net": "3V3", "expected_domain": "1V8"},
    )
    review = ReviewItem(
        id="SYNTHETIC_REVIEW_1",
        kind="ambiguity",
        question="synthetic self-check review item",
        options=["accept", "defer", "reject"],
        blocking=False,
    )
    events = [
        stage_event(
            {
                "stage": "IDENTIFY",
                "status": Status.PASS,
                "detail": "synthetic self-check stage",
                "elapsed_s": 0.0,
                "test_counts": {"PASS": 1},
                "artifacts": ["runs/synthetic/"],
            }
        ),
        progress_event("IDENTIFY", 1, 10, "synthetic self-check progress"),
        findings_event([finding]),
        review_event([review]),
        waveform_event("runs/synthetic/probe.raw", ["V(out)"], []),
        result_event({"status": Status.PASS, "results": results, "findings": [finding]}),
    ]
    return events


# --------------------------------------------------------------------------- #
# entry point


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m boardmodeler.pipeline.worker",
        description="Run one pipeline request and emit protocol events on stdout.",
    )
    parser.add_argument("--request", type=Path, default=None, help="request JSON file")
    parser.add_argument("--project", type=Path, default=None, help="project directory")
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="emit the documented event shapes from a synthetic result and exit",
    )
    parser.add_argument(
        "--controller-module",
        default=CONTROLLER_MODULE,
        help=f"module providing PipelineController/PipelineRequest (default: {CONTROLLER_MODULE})",
    )
    return parser.parse_args(argv)


def _load_controller_module(name: str) -> ModuleType:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        raise RequestError(
            "controller_unavailable", f"cannot import controller module {name!r}: {exc}"
        ) from exc
    for attribute in ("PipelineController", "PipelineRequest"):
        if not hasattr(module, attribute):
            raise RequestError(
                "controller_unavailable",
                f"controller module {name!r} has no {attribute}",
            )
    return module


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    """Run one request. Returns the process exit code (see the module docstring)."""
    args = _parse_args(argv)
    writer = EventWriter(stdout)

    if args.list_events:
        for event in synthetic_events():
            writer.emit(event)
        return EXIT_OK

    if args.request is None:
        writer.emit(error_event("request_invalid", "--request is required unless --list-events"))
        return EXIT_REQUEST_ERROR

    cancel = threading.Event()
    installed = _install_signal_handlers(writer, cancel)

    try:
        payload = load_request_payload(args.request)
        module = _load_controller_module(args.controller_module)
        if args.project is not None:
            payload = {**payload, "project_dir": str(args.project)}
        request = build_request(module.PipelineRequest, payload, project_dir=args.project)

        from boardmodeler.config import load_config

        try:
            config = load_config()
        except Exception as exc:
            raise RequestError("config_invalid", f"cannot load configuration: {exc}") from exc

        controller = module.PipelineController(config=config)
        run_request(
            controller,
            request,
            writer,
            cancel=cancel,
            stage_order=stage_sequence(module),
        )
    except SystemExit:
        raise
    except RequestError as exc:
        writer.emit(error_event(exc.code, exc.detail))
        return EXIT_REQUEST_ERROR
    except KeyboardInterrupt:  # pragma: no cover - delivered as SIGINT on Windows
        writer.emit(error_event("cancelled", "keyboard interrupt"))
        return EXIT_CANCELLED
    except BaseException as exc:
        print(f"worker internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        writer.emit(error_event("internal_error", f"{type(exc).__name__}: {exc}"))
        return EXIT_INTERNAL_ERROR
    finally:
        if installed:
            for signum in installed:
                with contextlib.suppress(ValueError, OSError, RuntimeError):
                    signal.signal(signum, signal.SIG_DFL)

    if cancel.is_set():
        writer.emit(error_event("cancelled", "run cancelled"))
        return EXIT_CANCELLED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
