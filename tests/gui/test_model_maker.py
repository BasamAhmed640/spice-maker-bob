"""The model maker window: it must show exactly what the engine reported, nothing more.

These tests run offscreen and stand in a fake engine module, so they verify the contract
between the window and ``pipeline.make_model`` (the attributes it reads, the table it
fills in, which buttons a result enables) rather than the engine's own behaviour, which
has its own tests.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@dataclass(frozen=True)
class _Row:
    req_id: str
    statement: str
    required: str
    measured: str
    status: str
    page: int | None


@dataclass(frozen=True)
class _Result:
    status: str
    detail: str
    part: str
    out_dir: Path
    card_path: Path | None
    lib_path: Path | None
    asy_path: Path | None
    rows: tuple[_Row, ...]
    counts: dict[str, int] = field(default_factory=dict)
    stages: tuple[object, ...] = ()

    def to_json(self) -> str:  # pragma: no cover - not read by the window
        return "{}"


@dataclass(frozen=True)
class _Request:
    part: str
    subckt: str
    datasheet: Path
    out_dir: Path
    backend_name: str = "bob"
    team_id: str | None = None
    max_iterations: int = 3
    timeout_s: float = 120.0
    allow_remote: bool = False
    provider: str | None = None
    requirements_json: Path | None = None
    bindings_json: Path | None = None


def _engine_module(result: _Result, calls: list[_Request]) -> ModuleType:
    module = ModuleType("boardmodeler.pipeline.make_model")

    def make_model(request: _Request, progress: object = None, cancel: object = None) -> _Result:
        calls.append(request)
        if callable(progress):
            for stage, status, detail in (
                ("read", "ok", "datasheet registered"),
                ("extract", "ok", "38 rows"),
                ("bind", "ok", "9 testable, 29 not testable"),
                ("author", "ok", "turn 1"),
                ("judge", "ok", "PASS 9"),
                ("save", "ok", "6 files"),
            ):
                progress(_Stage(stage, status, detail))
        return result

    module.make_model = make_model  # type: ignore[attr-defined]
    module.MakeModelRequest = _Request  # type: ignore[attr-defined]
    return module


@dataclass(frozen=True)
class _Stage:
    stage: str
    status: str
    detail: str
    counts: dict[str, int] = field(default_factory=dict)


def test_a_successful_run_fills_the_stage_and_row_tables(qtbot, tmp_path, monkeypatch) -> None:
    calls: list[_Request] = []
    result = _Result(
        status="PASS",
        detail="all 9 testable rows pass",
        part="TPS54320",
        out_dir=tmp_path,
        card_path=tmp_path / "MODEL_CARD.md",
        lib_path=tmp_path / "TPS54320.lib",
        asy_path=tmp_path / "TPS54320.asy",
        rows=(
            _Row(
                "REQ_1",
                "UVLO rising 4.0-4.5 V",
                "min 4 / max 4.5 V",
                "vin_uvlo_rise=4.21 V",
                "PASS",
                4,
            ),
            _Row(
                "REQ_2", "Output current 0-3 A", "min 0 / max 3 A", "i_out_limit=3.4 A", "FAIL", 3
            ),
            _Row("REQ_3", "Thermal shutdown", "no simulation probe", "-", "NOT_APPLICABLE", 9),
        ),
        counts={"PASS": 1, "FAIL": 1, "NOT_APPLICABLE": 1},
    )
    monkeypatch.setitem(
        sys.modules, "boardmodeler.pipeline.make_model", _engine_module(result, calls)
    )
    monkeypatch.setattr(
        "boardmodeler.ui.model_maker._agent_availability", lambda: (True, "test agent")
    )
    from boardmodeler import agent_providers

    monkeypatch.setattr(
        "boardmodeler.ui.model_maker._configured_provider", agent_providers.default_provider
    )

    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "tps54320.pdf"
    datasheet.write_bytes(b"%PDF-1.4 fake")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path / "out"))

    window.go_button.click()
    qtbot.waitUntil(lambda: window._result is not None, timeout=10_000)

    assert calls and calls[0].part == "TPS54320" and calls[0].subckt == "TPS54320"
    assert calls[0].backend_name == "api"
    # The window runs the build's own default provider, whatever this catalog holds.
    assert calls[0].provider == agent_providers.default_provider().id

    stages = {
        window.stages.item(r, 0).text(): window.stages.item(r, 1).text()
        for r in range(window.stages.rowCount())
    }
    assert {"read", "extract", "bind", "author", "judge", "save"} <= set(stages)
    assert window.stages.rowCount() == 6

    assert window.rows.rowCount() == 3
    assert [window.rows.item(r, 3).text() for r in range(3)] == ["PASS", "FAIL", "NOT_APPLICABLE"]
    assert window.rows.item(0, 2).text() == "vin_uvlo_rise=4.21 V"
    assert window.rows.item(2, 1).text() == "no simulation probe"

    assert window.status_label.text().startswith("PASS")
    assert window.install_button.isEnabled() is True
    assert window.open_button.isEnabled() is True
    assert window.cancel_button.isEnabled() is False


def test_a_blocked_run_still_reports_something_useful(qtbot, tmp_path, monkeypatch) -> None:
    calls: list[_Request] = []
    result = _Result(
        status="BLOCKED",
        detail="bob_shell_not_installed: install from https://bob.ibm.com/docs/shell",
        part="TPS54320",
        out_dir=tmp_path,
        card_path=None,
        lib_path=None,
        asy_path=None,
        rows=(_Row("REQ_1", "UVLO rising", "min 4 / max 4.5 V", "-", "UNKNOWN", 4),),
        counts={"UNKNOWN": 1},
    )
    monkeypatch.setitem(
        sys.modules, "boardmodeler.pipeline.make_model", _engine_module(result, calls)
    )
    monkeypatch.setattr(
        "boardmodeler.ui.model_maker._agent_availability", lambda: (True, "test agent")
    )
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "ds.pdf"
    datasheet.write_bytes(b"%PDF-1.4")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path))

    window.go_button.click()
    qtbot.waitUntil(lambda: window._result is not None, timeout=10_000)

    assert window.status_label.text().startswith("BLOCKED")
    assert "bob_shell_not_installed" in window.status_label.text()
    # nothing was produced: the install button must not offer something that is not there
    assert window.install_button.isEnabled() is False


def test_missing_inputs_never_start_a_run(qtbot, tmp_path, monkeypatch) -> None:
    calls: list[_Request] = []
    monkeypatch.setitem(
        sys.modules,
        "boardmodeler.pipeline.make_model",
        _engine_module(_Result("PASS", "", "X", tmp_path, None, None, None, ()), calls),
    )
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None, raising=True)

    window.part_edit.setText("")
    window.go_button.click()  # no part number
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(tmp_path / "does-not-exist.pdf"))
    window.go_button.click()  # datasheet does not exist

    assert calls == []
    assert window._result is None


def test_no_agent_available_stops_before_any_work(qtbot, tmp_path, monkeypatch) -> None:
    """The honest pre-flight: say what is missing instead of failing after a long run."""
    calls: list[_Request] = []
    monkeypatch.setitem(
        sys.modules,
        "boardmodeler.pipeline.make_model",
        _engine_module(_Result("PASS", "", "X", tmp_path, None, None, None, ()), calls),
    )
    monkeypatch.setattr(
        "boardmodeler.ui.model_maker._agent_availability",
        lambda: (False, "bob_credentials_unavailable: set BOB_API_KEY"),
    )
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda parent, title, text, *a, **k: shown.append((title, text)),
    )
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "ds.pdf"
    datasheet.write_bytes(b"%PDF-1.4")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path))

    window.go_button.click()

    assert calls == []
    assert window._result is None
    assert shown and shown[0][0] == "No agent available"
    assert "bob_credentials_unavailable" in shown[0][1]


def test_a_malformed_config_shows_the_blocked_dialog_instead_of_raising(
    qtbot, tmp_path, monkeypatch
) -> None:
    """A broken config file must degrade to the dialog, never escape the GO slot."""
    calls: list[_Request] = []
    monkeypatch.setitem(
        sys.modules,
        "boardmodeler.pipeline.make_model",
        _engine_module(_Result("PASS", "", "X", tmp_path, None, None, None, ()), calls),
    )
    import boardmodeler.config as config_module

    def broken_load(*args: object, **kwargs: object) -> object:
        raise ValueError("config.json is not valid JSON")

    monkeypatch.setattr(config_module, "load_config", broken_load)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda parent, title, text, *a, **k: shown.append((title, text)),
    )
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "ds.pdf"
    datasheet.write_bytes(b"%PDF-1.4")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path))

    window.go_button.click()

    assert calls == [], "no build may start from an unreadable config"
    assert shown and shown[0][0] == "No agent available"
