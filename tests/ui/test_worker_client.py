"""Worker client tests: event signals, outcome, process-tree cancellation."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from boardmodeler.ui.worker_client import WorkerClient

pytestmark = pytest.mark.gui

STUB_CONTROLLER = '''
"""Stub controller for the worker client tests."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import TestResult

STAGE_ORDER = ("IDENTIFY", "EVALUATE")


@dataclass
class PipelineRequest:
    project_dir: Path
    hold_s: float = 0.0


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
        if request.hold_s:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            Path("grandchild.pid").write_text(str(child.pid), encoding="utf-8")
        stages = []
        for index, stage in enumerate(STAGE_ORDER, start=1):
            if progress is not None:
                entry = StageProgress(
                    stage=stage,
                    status=Status.PASS,
                    detail=f"{stage} done",
                    elapsed_s=0.05 * index,
                    test_counts={"PASS": index},
                )
                stages.append(entry)
                progress(entry)
            if request.hold_s:
                deadline = time.monotonic() + request.hold_s
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        break
                    time.sleep(0.05)
        return PipelineResult(
            status=Status.PASS,
            stages=stages,
            results=[
                TestResult(
                    test_id="T1",
                    status=Status.PASS,
                    measured={"V(out)": 0.632},
                    expected="0.632 V",
                    detail="observed 0.632 V",
                )
            ],
            findings=[],
            review_items=[],
            artifacts=["runs/identify/"],
            diagnostics={"coverage": "1/1"},
        )
'''

BAD_STDOUT_CONTROLLER = '''
"""Stub controller that pollutes stdout to prove protocol violations surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boardmodeler.domain.enums import Status

print("this line is not a protocol event", flush=True)


@dataclass
class PipelineRequest:
    project_dir: Path


class PipelineController:
    def __init__(self, config: object | None = None) -> None:
        self.config = config

    def run(self, request: PipelineRequest, progress=None, cancel=None) -> Any:
        return {"status": Status.PASS, "results": [], "stages": []}
'''


@pytest.fixture
def stub_project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "stub_controller.py").write_text(STUB_CONTROLLER, encoding="utf-8")
    (tmp_path / "bad_stdout_controller.py").write_text(BAD_STDOUT_CONTROLLER, encoding="utf-8")
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + (os.pathsep + existing if existing else ""))
    return project


def _wait_for(predicate, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_client_reemits_every_event_and_records_the_outcome(qapp, stub_project: Path) -> None:
    client = WorkerClient(project_dir=stub_project, controller_module="stub_controller")
    seen: dict[str, list[dict]] = {}
    for name in ("stage", "progress", "result", "findings", "review", "error"):
        signal = getattr(client, name)
        signal.connect(lambda event, key=name: seen.setdefault(key, []).append(event))

    client.start({"project_dir": str(stub_project)})
    outcome = client.wait(120)
    qapp.processEvents()  # queued signal deliveries land on the Qt event loop

    assert outcome.exit_code == 0
    assert outcome.ok
    assert outcome.status == "PASS"
    assert outcome.summary == {"PASS": 1}
    assert [row["test_id"] for row in outcome.results] == ["T1"]
    assert [event["stage"] for event in outcome.stages] == ["IDENTIFY", "EVALUATE"]
    assert [event["stage"] for event in seen["stage"]] == ["IDENTIFY", "EVALUATE"]
    assert seen["progress"][-1]["total"] == 2
    assert seen["result"][0]["status"] == "PASS"
    assert outcome.malformed_lines == []
    assert outcome.request_path is not None and outcome.request_path.is_file()


def test_client_cancel_terminates_the_whole_process_tree(qapp, stub_project: Path) -> None:
    import psutil

    client = WorkerClient(
        project_dir=stub_project, cancel_timeout_s=5.0, controller_module="stub_controller"
    )
    client.start({"project_dir": str(stub_project), "hold_s": 120.0})
    assert _wait_for(lambda: (stub_project / "grandchild.pid").is_file()), (
        "grandchild never started"
    )
    grandchild = int((stub_project / "grandchild.pid").read_text(encoding="utf-8"))
    assert psutil.pid_exists(grandchild)

    assert client.cancel() is True
    outcome = client.wait(60)

    assert outcome.cancelled
    assert not client.is_running()
    assert _wait_for(lambda: not psutil.pid_exists(grandchild), timeout_s=15.0), (
        "the grandchild survived cancel(); the process tree was not killed"
    )


def test_client_surfaces_protocol_violations(qapp, stub_project: Path) -> None:
    client = WorkerClient(project_dir=stub_project, controller_module="bad_stdout_controller")
    client.start({"project_dir": str(stub_project)})
    client.wait(120)
    assert any("not a protocol event" in line for line in client.outcome.malformed_lines)
    assert client.outcome.exit_code == 0  # the junk line was recorded, not fatal


def test_client_resume_reruns_the_last_request(qapp, stub_project: Path) -> None:
    client = WorkerClient(project_dir=stub_project, controller_module="stub_controller")
    started: list[dict] = []
    client.started.connect(started.append)
    client.start({"project_dir": str(stub_project)})
    client.wait(120)
    assert len(started) == 1

    assert client.resume() is True
    client.wait(120)
    assert len(started) == 2
    assert client.outcome.exit_code == 0
    assert client.last_request["project_dir"] == str(stub_project)


def test_jsonable_request_handles_paths_and_enums() -> None:
    from dataclasses import dataclass

    from boardmodeler.domain.enums import Status
    from boardmodeler.ui.worker_client import jsonable_request

    @dataclass
    class Request:
        project_dir: Path
        status: Status

    payload = jsonable_request(Request(project_dir=Path("C:/tmp/p"), status=Status.PASS))
    assert payload == {"project_dir": str(Path("C:/tmp/p")), "status": "PASS"}
