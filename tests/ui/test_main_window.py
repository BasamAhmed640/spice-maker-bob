"""Main window tests: inputs, worker events -> views, export, review, settings."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from boardmodeler.domain.records import ReviewItem
from boardmodeler.pipeline.project import create_project
from boardmodeler.simulation.ltspice import SmokeResult
from boardmodeler.ui.main_window import DEFAULT_USE_PROFILE, MainWindow
from boardmodeler.ui.worker_client import jsonable_request

pytestmark = pytest.mark.gui


class FakeWorker(QObject):
    """Stands in for :class:`WorkerClient`: same signals, scripted by tests."""

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

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict] = []
        self.projects: list[Path] = []
        self.running = False
        self.cancel_calls = 0
        self.outcome = SimpleNamespace(cancelled=False, exit_code=None)

    def is_running(self) -> bool:
        return self.running

    def start(self, request, *, project_dir=None) -> Path:
        self.requests.append(jsonable_request(request))
        self.projects.append(Path(project_dir) if project_dir is not None else Path("."))
        self.running = True
        return Path("runs") / "ui-request.json"

    def cancel(self) -> bool:
        self.cancel_calls += 1
        self.running = False
        self.outcome.cancelled = True
        return True


def _make_project(tmp_path: Path, *, circuit: bool = True):
    project = create_project(tmp_path / "proj", project_id="P1", name="Demo board", mode="circuit")
    if circuit:
        (project.root / "circuit").mkdir(exist_ok=True)
        (project.root / "circuit" / "demo.asc").write_text("Version 4\n", encoding="utf-8")
    return project


def _window(fake: FakeWorker, tmp_path: Path) -> MainWindow:
    return MainWindow(worker_factory=lambda: fake, config_file=tmp_path / "config.json")


def _stage(stage: str, status: str, detail: str = "ok") -> dict:
    return {
        "event": "stage",
        "stage": stage,
        "status": status,
        "detail": detail,
        "elapsed_s": 0.25,
        "test_counts": {"PASS": 1},
        "artifacts": ["runs/x/"],
    }


def test_load_project_populates_inputs_and_title(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    window = _window(FakeWorker(), tmp_path)
    assert window.load_project(project.root) is True

    assert window.project_dir() == project.root
    assert "Demo board" in window.windowTitle()
    assert window.inputs.project_dir() == project.root
    assert window.inputs.mode.currentText() == "circuit"
    assert window.inputs.use_profile_text() == DEFAULT_USE_PROFILE
    assert window.inputs.circuit_file() == project.root / "circuit" / "demo.asc"


def test_load_project_rejects_a_plain_directory(qapp, tmp_path: Path) -> None:
    plain = tmp_path / "not-a-project"
    plain.mkdir()
    window = _window(FakeWorker(), tmp_path)
    assert window.load_project(plain) is False
    assert "project.json" in window.statusBar().currentMessage()


def test_start_run_sends_interface_request_fields(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)

    assert window.start_run() is True
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request["project_dir"] == str(project.root)
    assert request["mode"] == "circuit"
    assert request["use_profile"] == DEFAULT_USE_PROFILE
    assert request["allow_remote"] is False
    assert str(project.root / "circuit" / "demo.asc") in request["document_paths"]
    assert "export_dir" not in request
    assert "part_identity" not in request
    assert fake.projects == [project.root]

    window.inputs.part_manufacturer.setText("Texas Instruments")
    window.inputs.part_ordering_code.setText("TPS54320")
    payload = window.request_payload()
    assert payload["part_identity"] == {
        "manufacturer": "Texas Instruments",
        "ordering_code": "TPS54320",
        "base_part": "TPS54320",
    }


def test_worker_events_populate_stage_list_and_results(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    window.start_run()

    fake.stage.emit(_stage("IDENTIFY", "PASS", "documents read"))
    fake.stage.emit(_stage("EVALUATE", "FAIL", "V(out)=1.5 V"))
    fake.progress.emit(
        {"event": "progress", "stage": "EVALUATE", "done": 2, "total": 10, "detail": ""}
    )
    fake.findings.emit(
        {
            "event": "findings",
            "findings": [
                {
                    "code": "SC001_syntax",
                    "status": "PASS",
                    "refdes": "R1",
                    "nets": [],
                    "message": "fine",
                },
                {
                    "code": "SC009_supply_domain_assignment",
                    "status": "FAIL",
                    "refdes": "U1",
                    "nets": ["3V3"],
                    "message": "PG on the wrong rail",
                    "detail": {"observed_net": "3V3"},
                },
            ],
        }
    )
    fake.review.emit(
        {"event": "review", "items": [{"id": "R1", "kind": "conflict", "question": "?"}]}
    )
    fake.result.emit(
        {
            "event": "result",
            "status": "FAIL",
            "summary": {"PASS": 1, "FAIL": 1},
            "results": [
                {
                    "test_id": "T1",
                    "status": "PASS",
                    "measured": {"V(out)": 0.632},
                    "expected": "V(out)=0.632",
                    "detail": "ok",
                },
                {
                    "test_id": "T2",
                    "status": "FAIL",
                    "measured": {"V(out)": 1.5},
                    "expected": "V(out)<1.0",
                    "detail": "observed 1.5 V",
                },
                {
                    "test_id": "T3",
                    "status": "UNKNOWN",
                    "measured": {},
                    "expected": "measurement",
                    "detail": "no measurement",
                    "unknown_reason": "missing_measurement",
                },
            ],
            "diagnostics": {
                "coverage": "2/3 requirements covered",
                "evidence": "datasheet page 5",
                "assumption": "REFCLK availability assumed",
                "notes": "n/a",
            },
            "artifacts": ["runs/x/deck.cir"],
            "manifest": "runs/x/manifest.json",
        }
    )

    assert window.stage_list().count() == 2
    assert [row["status"] for row in window.stage_rows()] == ["PASS", "FAIL"]
    assert window.progress_bar.value() == 2 and window.progress_bar.maximum() == 10

    findings = window.findings_list().rows()
    assert [row["code"] for row in findings] == ["SC009_supply_domain_assignment", "SC001_syntax"]
    assert window.review_panel().items()[0]["id"] == "R1"

    table = window.results_table()
    assert [row["test_id"] for row in table.rows()] == ["T1", "T2", "T3"]
    assert table.statuses() == ["PASS", "FAIL", "UNKNOWN"]
    assert "FAIL" in window.results_panel.summary.text()

    groups = window.results_panel.detail_group_names()
    assert {"Coverage", "Evidence", "Assumptions", "Diagnostics", "Artifacts"} <= set(groups)


def test_repeated_stage_updates_one_row(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    window.start_run()

    fake.stage.emit(_stage("REPAIR", "UNKNOWN", "attempt 1"))
    fake.stage.emit(_stage("REPAIR", "PASS", "attempt 2"))
    assert window.stage_list().count() == 1
    assert window.stage_rows()[0]["status"] == "PASS"
    assert "attempt 2" in window.stage_list().item(0).text()


def test_cancel_run_and_exit_update_the_buttons(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    window.start_run()
    assert window.cancel_button.isEnabled()

    assert window.cancel_run() is True
    assert fake.cancel_calls == 1
    assert window.cancel_run() is False  # nothing running any more

    fake.exited.emit(130)
    assert not window.cancel_button.isEnabled()
    assert window.inputs.run_button.isEnabled()


def test_second_run_is_refused_while_one_is_running(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)

    assert window.start_run() is True
    assert window.start_run() is False
    assert "already in progress" in window.statusBar().currentMessage()
    assert len(fake.requests) == 1


def test_export_project_requests_an_export_directory(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    target = tmp_path / "exported"

    assert window.export_project(target) is True
    assert fake.requests[-1]["export_dir"] == str(target)
    assert window.export_target() == target
    assert window.export_project() is False  # a run is already in progress


def test_error_event_is_recorded_and_shown(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    window.start_run()

    fake.error.emit({"event": "error", "code": "project_not_found", "detail": "gone"})
    assert window.last_error_event["code"] == "project_not_found"
    assert "project_not_found" in window.statusBar().currentMessage()


def test_waveform_event_adds_a_ref_and_loads_traces(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    runs = project.root / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "probe.raw").write_text(
        "Title: * synthetic\nPlotname: Transient Analysis\nFlags: real\n"
        "No. Variables: 2\nNo. Points: 2\nVariables:\n"
        "\t0\ttime\ttime\n\t1\tV(out)\tvoltage\nValues:\n0\t0.0\t0.0\n1\t1e-3\t0.632\n",
        encoding="utf-8",
    )
    fake = FakeWorker()
    window = _window(fake, tmp_path)
    window.load_project(project.root)
    window.start_run()

    fake.waveform.emit(
        {
            "event": "waveform",
            "ref": "runs/r1/probe.raw",
            "signals": ["time", "V(out)"],
            "violations": [{"req_id": "REQ_PG_001", "t_s": 5e-4}],
        }
    )

    assert window.waveform_refs() == ["runs/r1/probe.raw"]
    assert window.waveform_view().trace_names() == ["V(out)"]
    markers = window.waveform_view().violation_markers()
    assert [(m.req_id, m.t_s) for m in markers] == [("REQ_PG_001", 5e-4)]


def test_review_resolution_is_written_into_review_json(qapp, tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    items = [
        ReviewItem(id="Q1", kind="ambiguity", question="which part?", options=["a", "b"]),
        ReviewItem(id="Q2", kind="conflict", question="limit?", blocking=True),
    ]
    (project.root / "review.json").write_text(
        json.dumps([json.loads(item.model_dump_json()) for item in items], indent=2),
        encoding="utf-8",
    )

    window = _window(FakeWorker(), tmp_path)
    assert window.load_project(project.root) is True
    panel = window.review_panel()
    assert [item["id"] for item in panel.items()] == ["Q1", "Q2"]
    assert [item["id"] for item in panel.blocking_items()] == ["Q2"]

    assert panel.resolve("Q1", "accepted") is True
    assert panel.resolve("NOPE", "deferred") is False
    on_disk = json.loads((project.root / "review.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in on_disk}
    assert by_id["Q1"]["resolution"] == "accepted"
    assert by_id["Q2"]["resolution"] is None
    assert panel.unresolved_items()[0]["id"] == "Q2"


def test_open_in_ltspice_hands_the_deck_to_the_desktop(qapp, tmp_path: Path, monkeypatch) -> None:
    project = _make_project(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(
        "boardmodeler.ui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    window = _window(FakeWorker(), tmp_path)
    window.load_project(project.root)

    assert window.open_in_ltspice() is True
    assert [Path(path) for path in opened] == [project.root / "circuit" / "demo.asc"]

    empty = create_project(tmp_path / "empty", project_id="P2", name="No circuit")
    window2 = _window(FakeWorker(), tmp_path)
    window2.load_project(empty.root)
    assert window2.open_in_ltspice() is False


def test_settings_dialog_round_trip_and_smoke_result(qapp, tmp_path: Path, monkeypatch) -> None:
    from boardmodeler.ui.settings import SettingsDialog

    config_file = tmp_path / "config.json"
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "boardmodeler.ui.settings.set_credential",
        lambda name, value: stored.append((name, value)),
    )
    monkeypatch.setattr(
        "boardmodeler.ui.settings.locate_outcome",
        lambda explicit: SimpleNamespace(
            install=SimpleNamespace(path=Path("C:/fake/LTspice.exe")), probed_paths=["C:/fake"]
        ),
    )
    monkeypatch.setattr(
        "boardmodeler.ui.settings.smoke_test",
        lambda exe, workdir, timeout_s=60.0: SmokeResult(
            status="pass",
            detail="V(out)@1e-03s = 0.632 V (analytic 0.632 V)",
            measured_v=0.632,
            expected_v=0.632,
            tolerance_pct=2.0,
        ),
    )

    dialog = SettingsDialog(config_file=config_file)
    dialog.provider.setCurrentText("http_inference")
    dialog.endpoint.setText("https://api.example.invalid/v1")
    dialog.model_name.setText("example-model")
    dialog.secret.setText("sk-top-secret")
    assert "sk-top-secret" not in json.dumps(dialog.values())

    assert "stored in the encrypted local file" in dialog.store_credential()
    assert stored == [("http_inference", "sk-top-secret")]
    assert dialog.secret.text() == ""

    observed = dialog.run_smoke_test()
    assert observed["status"] == "pass"
    assert dialog.smoke_label.text().startswith("PASS")

    written = dialog.save()
    assert written == config_file
    payload = json.loads(config_file.read_text(encoding="utf-8"))
    assert payload["providers"]["http_inference"]["endpoint"] == "https://api.example.invalid/v1"
    assert payload["providers"]["http_inference"]["model"] == "example-model"
    assert "sk-top-secret" not in config_file.read_text(encoding="utf-8")


def test_settings_dialog_reports_a_failed_smoke_test(qapp, tmp_path: Path, monkeypatch) -> None:
    from boardmodeler.ui.settings import SettingsDialog

    monkeypatch.setattr(
        "boardmodeler.ui.settings.locate_outcome",
        lambda explicit: SimpleNamespace(install=None, probed_paths=["C:/one", "C:/two"]),
    )
    dialog = SettingsDialog(config_file=tmp_path / "config.json")
    observed = dialog.run_smoke_test()
    assert observed["status"] == "fail"
    assert "C:/one" in observed["detail"]
    assert dialog.smoke_label.text().startswith("FAIL")


def test_real_worker_run_renders_offscreen(qapp, tmp_path: Path) -> None:
    """The window drives the real worker module and renders its real events."""
    project = _make_project(tmp_path, circuit=False)
    window = MainWindow(config_file=tmp_path / "config.json")
    assert window.load_project(project.root) is True
    assert window.start_run() is True

    client = window.worker_client()
    assert client is not None
    outcome = client.wait(300)
    qapp.processEvents()

    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.result is not None
    assert window.last_result_event is not None
    assert window.stage_list().count() >= 1
    assert next(row["stage"] for row in window.stage_rows()) == "IDENTIFY"
    assert window.results_table().rowCount() == len(outcome.results)
    assert window.statusBar().currentMessage()
