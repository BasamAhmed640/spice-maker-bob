"""Worker protocol tests (INTERFACES §2).

The controller is not required to exist: every test drives the worker through a
stub controller module, which is exactly the lazy-import seam the protocol is
built on.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from io import StringIO
from pathlib import Path

import pytest

from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import TestResult
from boardmodeler.pipeline import worker

STUB_CONTROLLER = '''
"""Stub controller used by the worker protocol tests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import Finding, ReviewItem, TestResult

STAGE_ORDER = ("IDENTIFY", "EVALUATE")


@dataclass
class PipelineRequest:
    project_dir: Path
    mode: Literal["component", "circuit"] = "component"
    document_paths: list[Path] = field(default_factory=list)
    allow_remote: bool = False
    export_dir: Path | None = None
    sleep_s: float = 0.0


@dataclass
class StageProgress:
    stage: Any
    status: Status
    detail: str = ""
    elapsed_s: float = 0.0
    test_counts: dict[str, int] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    status: Status
    stages: list[Any] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)
    review_items: list[Any] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    manifest_path: Path | None = None
    export_dir: Path | None = None
    diagnostics: dict[str, str] = field(default_factory=dict)


class PipelineController:
    def __init__(self, config: object | None = None) -> None:
        self.config = config

    def run(self, request: PipelineRequest, progress=None, cancel=None) -> PipelineResult:
        stages = []
        for index, stage in enumerate(STAGE_ORDER, start=1):
            if index > 1 and request.sleep_s:
                deadline = time.monotonic() + request.sleep_s
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        break
                    time.sleep(0.02)
            entry = StageProgress(
                stage=stage,
                status=Status.PASS,
                detail=f"{stage} done",
                elapsed_s=0.1 * index,
                test_counts={"PASS": index},
                artifacts=[f"runs/{stage.lower()}/"],
            )
            stages.append(entry)
            if progress is not None:
                progress(entry)
        results = [
            TestResult(
                test_id="T1",
                status=Status.FAIL,
                measured={"V(out)": 1.5},
                expected="V(out) < 1.0",
                detail="observed 1.5 V",
            )
        ]
        findings = [
            Finding(
                code="SC009_supply_domain_assignment",
                status=Status.FAIL,
                refdes="U1",
                nets=["3V3"],
                message="U1 PG is on 3V3 but declares 1V8",
                detail={"observed_net": "3V3"},
            )
        ]
        review = [ReviewItem(id="R1", kind="conflict", question="which rail?", options=["a", "b"])]
        return PipelineResult(
            status=Status.FAIL,
            stages=stages,
            results=results,
            findings=findings,
            review_items=review,
            artifacts=["runs/identify/", "runs/evaluate/"],
            manifest_path=Path("runs/manifest.json"),
            diagnostics={"coverage": "1/2 requirements covered"},
        )
'''


@pytest.fixture
def stub_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A directory holding ``stub_controller.py`` plus the env to import it."""
    (tmp_path / "stub_controller.py").write_text(STUB_CONTROLLER, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(tmp_path) + (os.pathsep + existing if existing else "")
    return tmp_path, env


def _run_worker(
    *args: str, cwd: Path, env: dict[str, str], timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "boardmodeler.pipeline.worker", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _events(stdout: str) -> list[dict]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# --------------------------------------------------------------------------- #
# framing and the self-check mode


def test_list_events_emits_only_documented_shapes() -> None:
    stream = StringIO()
    assert worker.main(["--list-events"], stdout=stream) == worker.EXIT_OK

    events = _events(stream.getvalue())
    kinds = [event["event"] for event in events]
    assert set(kinds) <= set(worker.EVENT_KINDS)
    assert set(kinds) == set(worker.EVENT_KINDS) - {"error"}

    by_kind = {event["event"]: event for event in events}
    assert set(by_kind["stage"]) >= {
        "event",
        "stage",
        "status",
        "detail",
        "elapsed_s",
        "test_counts",
        "artifacts",
    }
    assert set(by_kind["progress"]) == {"event", "stage", "done", "total", "detail"}
    assert set(by_kind["findings"]) == {"event", "findings"}
    assert set(by_kind["review"]) == {"event", "items"}
    assert set(by_kind["waveform"]) == {"event", "ref", "signals", "violations"}
    assert set(by_kind["result"]) >= {"event", "status", "summary", "results"}

    summary = by_kind["result"]["summary"]
    assert summary == {"PASS": 1, "UNKNOWN": 1, "BLOCKED": 1}
    assert by_kind["waveform"]["ref"].endswith(".raw")
    assert by_kind["findings"]["findings"][0]["code"] == "SC009_supply_domain_assignment"


def test_list_events_is_one_json_object_per_line() -> None:
    stream = StringIO()
    worker.main(["--list-events"], stdout=stream)
    for line in stream.getvalue().splitlines():
        decoded = json.loads(line)
        assert isinstance(decoded, dict)
        assert "event" in decoded


# --------------------------------------------------------------------------- #
# end-to-end subprocess run with a stub controller


def test_worker_runs_end_to_end_and_reports_events(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    (tmp_path / "project" / "project.json").write_text("{}", encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"project_dir": str(tmp_path / "project"), "allow_remote": True}),
        encoding="utf-8",
    )

    result = _run_worker(
        "--request",
        str(request),
        "--project",
        str(tmp_path / "project"),
        "--controller-module",
        "stub_controller",
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == worker.EXIT_OK, result.stderr
    events = _events(result.stdout)
    kinds = [event["event"] for event in events]
    assert kinds[:2] == ["stage", "progress"]
    assert kinds[-1] == "result"
    assert kinds.count("stage") == 2
    assert "findings" in kinds and "review" in kinds

    result_event = events[-1]
    assert result_event["status"] == Status.FAIL.value
    assert result_event["summary"] == {"FAIL": 1}
    assert result_event["results"][0]["test_id"] == "T1"
    assert result_event["results"][0]["measured"] == {"V(out)": 1.5}
    assert result_event["manifest"] == str(Path("runs") / "manifest.json")
    assert result_event["diagnostics"] == {"coverage": "1/2 requirements covered"}
    assert result_event["stages"][0]["stage"] == "IDENTIFY"
    assert "stage" not in result.stderr

    # stdout discipline: every line is one complete JSON object, nothing else
    assert all(line.startswith("{") and line.endswith("}") for line in result.stdout.splitlines())
    # the request round-tripped through the dataclass without dropping fields
    assert events[1]["total"] == 2


def test_worker_rejects_unknown_request_fields(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"project_dir": str(tmp_path / "project"), "allow_remot": True}),
        encoding="utf-8",
    )
    result = _run_worker(
        "--request", str(request), "--controller-module", "stub_controller", cwd=tmp_path, env=env
    )
    assert result.returncode == worker.EXIT_REQUEST_ERROR
    assert _events(result.stdout) == [
        {
            "event": "error",
            "code": "request_invalid",
            "detail": "unknown request field(s): allow_remot",
        }
    ]


def test_worker_reports_missing_project(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"project_dir": str(tmp_path / "nope")}), encoding="utf-8")
    result = _run_worker(
        "--request", str(request), "--controller-module", "stub_controller", cwd=tmp_path, env=env
    )
    assert result.returncode == worker.EXIT_REQUEST_ERROR
    assert _events(result.stdout)[0]["code"] == "project_not_found"


def test_worker_reports_unreadable_request(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    result = _run_worker(
        "--request",
        str(tmp_path / "absent.json"),
        "--controller-module",
        "stub_controller",
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == worker.EXIT_REQUEST_ERROR
    assert _events(result.stdout)[0]["code"] == "request_unreadable"


def test_worker_reports_invalid_request_json(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    request = tmp_path / "request.json"
    request.write_text("{not json", encoding="utf-8")
    result = _run_worker(
        "--request", str(request), "--controller-module", "stub_controller", cwd=tmp_path, env=env
    )
    assert result.returncode == worker.EXIT_REQUEST_ERROR
    assert _events(result.stdout)[0]["code"] == "request_invalid"


def test_worker_reports_missing_controller(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"project_dir": str(tmp_path / "project")}), encoding="utf-8")
    result = _run_worker(
        "--request",
        str(request),
        "--controller-module",
        "boardmodeler.pipeline.no_such_controller",
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == worker.EXIT_REQUEST_ERROR
    assert _events(result.stdout)[0]["code"] == "controller_unavailable"


def test_worker_requires_request_or_list_events() -> None:
    stream = StringIO()
    assert worker.main([], stdout=stream) == worker.EXIT_REQUEST_ERROR
    assert _events(stream.getvalue())[0]["code"] == "request_invalid"


# --------------------------------------------------------------------------- #
# cancellation


def test_cancel_handler_emits_cancelled_and_exits_130() -> None:
    stream = StringIO()
    cancel = threading.Event()
    handler = worker.make_cancel_handler(worker.EventWriter(stream), cancel)

    with pytest.raises(SystemExit) as excinfo:
        handler(signal.SIGINT, None)

    assert excinfo.value.code == worker.EXIT_CANCELLED == 130
    assert cancel.is_set()
    assert _events(stream.getvalue()) == [
        {"event": "error", "code": "cancelled", "detail": "received signal 2"}
    ]

    # a second signal must not duplicate the event
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)
    assert len(_events(stream.getvalue())) == 1


@pytest.mark.skipif(os.name != "nt", reason="CTRL_BREAK_EVENT is Windows-only")
def test_worker_honours_ctrl_break(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    (tmp_path / "project").mkdir(exist_ok=True)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"project_dir": str(tmp_path / "project"), "sleep_s": 30.0}),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "boardmodeler.pipeline.worker",
            "--request",
            str(request),
            "--controller-module",
            "stub_controller",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        assert proc.stdout is not None
        # wait for the first stage event so the handler is installed and running
        first = proc.stdout.readline()
        assert json.loads(first)["event"] == "stage"
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == worker.EXIT_CANCELLED, (stdout, stderr)
    tail = _events(stdout)
    assert tail[-1]["event"] == "error"
    assert tail[-1]["code"] == "cancelled"


# --------------------------------------------------------------------------- #
# helpers used by the GUI and by tests


def test_build_request_coerces_paths_and_identity(tmp_path: Path) -> None:
    from dataclasses import dataclass, field

    @dataclass
    class Request:
        project_dir: Path
        document_paths: list[Path] = field(default_factory=list)
        part_identity: object | None = None
        mode: str = "component"

    request = worker.build_request(
        Request,
        {
            "project_dir": str(tmp_path),
            "document_paths": [str(tmp_path / "a.pdf")],
            "part_identity": {
                "manufacturer": "TI",
                "family": "TPS54320",
                "ordering_code": "TPS54320",
            },
            "mode": "circuit",
        },
    )
    assert request.project_dir == tmp_path
    assert request.document_paths == [tmp_path / "a.pdf"]
    assert request.part_identity.ordering_code == "TPS54320"
    assert request.mode == "circuit"


def test_cli_project_flag_wins_over_request_payload(stub_env: tuple[Path, dict[str, str]]) -> None:
    tmp_path, env = stub_env
    other = tmp_path / "other"
    other.mkdir()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"project_dir": str(tmp_path / "project")}), encoding="utf-8")
    result = _run_worker(
        "--request",
        str(request),
        "--project",
        str(other),
        "--controller-module",
        "stub_controller",
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == worker.EXIT_OK, result.stderr


def test_emit_waveforms_reads_signal_names_from_a_raw_file(tmp_path: Path) -> None:
    raw = tmp_path / "probe.raw"
    raw.write_text(
        "Title: * synthetic\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        "No. Variables: 2\n"
        "No. Points: 2\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tV(out)\tvoltage\n"
        "Values:\n"
        "0\t0.0\t0.0\n"
        "1\t1e-3\t0.632\n",
        encoding="utf-8",
    )
    result = TestResult(
        test_id="T-wave",
        status=Status.PASS,
        measured={"V(out)": 0.632},
        expected="0.632 V",
        waveform_refs=["probe.raw", "probe.raw", "missing.raw"],
    )
    stream = StringIO()
    worker.emit_waveforms(worker.EventWriter(stream), {"results": [result]}, tmp_path)

    events = _events(stream.getvalue())
    assert [event["ref"] for event in events] == ["probe.raw", "missing.raw"]
    assert events[0]["signals"] == ["time", "V(out)"]
    assert events[1]["signals"] == []
