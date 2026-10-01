"""Every button on every surface, pressed once, with nothing allowed to raise.

``tests/gui/`` used to assert rendered pixel colours and signal wiring and never click
anything, so a button wired to a slot that raises on its first press passed the whole
suite. This wrapper runs the sweep in ``tools/gui_sweep.py`` and asserts what the owner's
requirement actually needs: no click raised, no traceback was captured by Qt, every
surface was discovered and opened, every enabled button is wired to something and did
something observable, and every checkbox returned to where it started.

The sweep sandboxes every side effect before the first click — file dialogs, message
boxes, the worker child process, the CLI, the LTspice probes, the credential environment
variables and every config/model/credential path — so running it here touches nothing
real and spawns nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

REPO_ROOT = Path(__file__).resolve().parents[2]
SWEEP_PATH = REPO_ROOT / "tools" / "gui_sweep.py"

#: The generous variant pumps the event loop for a second per click; measured at ~68 s
#: for all five surfaces, which is why it is opt-in and the suite stays fast without it.
_FULL_PUMP: dict[str, int] = {"pump_iterations": 20, "pump_ms": 50}


def _load_sweep() -> Any:
    """Load ``tools/gui_sweep.py`` by path.

    ``tools/`` is not a package and is not on ``sys.path`` (no other test imports from
    it), so the module is loaded from its file the way
    ``tests/pipeline/test_baseline_freeze.py`` loads a sibling it cannot import.
    """
    spec = importlib.util.spec_from_file_location("boardmodeler_gui_sweep", SWEEP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load_sweep()


@pytest.fixture(scope="module")
def report(qapp: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One sweep for the whole module: ~40 clicks, about two seconds."""
    return sweep.run_sweep(sandbox_root=tmp_path_factory.mktemp("gui-sweep"))


def _controls(sweep_report: Any) -> list[dict[str, Any]]:
    return [control for surface in sweep_report.surfaces for control in surface["controls"]]


def test_the_sweep_reports_every_surface(report: Any) -> None:
    """A surface that silently stopped being swept would be the same as no coverage."""
    assert [surface["name"] for surface in report.surfaces] == list(sweep.SURFACE_NAMES)
    for surface in report.surfaces:
        assert surface["opened"] is True, surface["skip_reason"]
        assert surface["controls"], f"{surface['name']} exposed no buttons at all"
        width, height = surface["minimum_size"]
        assert width > 0 and height > 0, (
            f"{surface['name']} reports a zero minimum size ({surface['minimum_size']}); "
            "a fixed-size window would show up here as minimum == maximum"
        )
        assert surface["minimum_size"] != surface["size_hint"] or surface["size_hint"] != [
            0,
            0,
        ], f"{surface['name']} reports no usable sizes"


def test_no_click_raised(report: Any) -> None:
    """The owner's requirement, literally: pressing every button must not raise."""
    assert report.totals["click_errors"] == 0, [
        (control["label"], control["error"]) for control in _controls(report) if control["error"]
    ]
    for surface in report.surfaces:
        assert surface["errors"] == [], surface["errors"]


def test_no_traceback_was_captured(report: Any) -> None:
    """PySide6 reports a slot's exception through ``sys.excepthook`` and carries on."""
    assert report.captured_tracebacks == [], report.captured_tracebacks
    assert report.totals["qt_critical"] == 0, report.qt_messages


def test_default_behavioral_go_reaches_the_sandbox_without_starting_a_thread(report: Any) -> None:
    surface = report.surface("ModelMakerWindow")
    assert surface is not None
    go = next(control for control in surface["controls"] if control["label"] == "Make Model")
    assert go["clicked"] and go["error"] is None
    assert "MakeModelWorker.start requested (sandboxed: no thread runs)" in go["effects"]
    assert report.sandbox["kinds"]["thread_start"] >= 1


def test_every_enabled_button_is_wired_and_observably_did_something(report: Any) -> None:
    """A button with no connection and no effect is a button that does nothing."""
    assert report.failures == [], report.failures
    assert report.totals["unhandled"] == 0
    for control in _controls(report):
        if control["clicked"]:
            assert control["connected"] or control["effects"], control
        if control["enabled"] and not control["clicked"]:
            assert control["skipped"], f"{control['label']} was neither clicked nor explained"


def test_checkbox_round_trip_holds(report: Any) -> None:
    """A checkbox is a switch: the second press must land back where the first started."""
    checkable = [
        control for control in _controls(report) if control["checkable"] and control["clicked"]
    ]
    assert checkable, "the sweep must have clicked at least one checkbox"
    for control in checkable:
        assert control["checked_after_first"] != control["checked_before"], (
            f"{control['label']} did not change on the first press, so the round trip "
            "would prove nothing"
        )
        assert control["round_trip"] is True, control
    assert report.totals["round_trips"] == report.totals["checkable_clicked"]


def test_the_dormant_surfaces_are_still_constructible(report: Any) -> None:
    """``MainWindow`` (no project) and ``SettingsDialog`` are reached by a flag only.

    They are the package's earlier surfaces: dormant, but shipped. If either stops being
    constructible nothing else in the suite would notice until a user passed
    ``--board-ui``.
    """
    for name in ("MainWindow", "SettingsDialog"):
        surface = report.surface(name)
        assert surface is not None, f"{name} was not reported at all"
        assert surface["skip_reason"] is None, surface["skip_reason"]
        assert surface["opened"] is True, f"{name} did not open"
        assert any(control["clicked"] for control in surface["controls"]), (
            f"{name} exposed nothing that could be clicked"
        )


def test_the_report_is_machine_readable(report: Any) -> None:
    payload = report.to_json()
    for key in ("invocation", "qt_platform", "surfaces", "totals", "failures"):
        assert key in payload, f"the report must carry {key!r}"
    assert json.loads(json.dumps(payload))["totals"] == payload["totals"]
    assert payload["qt_platform"] == "offscreen"
    assert payload["limitations"], "the report must state its own limitations"


def test_the_sweep_leaves_no_sandbox_stub_bound_in_the_application(report: Any) -> None:
    """A module imported *during* the sweep must not keep the sandbox's function.

    ``ModelMakerWindow`` imports the engine lazily, so the click on GO imports
    ``pipeline.make_model`` while the sandbox is installed; that module does
    ``from boardmodeler.simulation.ltspice import locate`` at import time, and without
    the repair in ``Sandbox.restore`` it would keep the sandbox's fake LTspice path for
    the rest of the process — which is exactly how this test found it.
    """
    from boardmodeler.simulation import ltspice

    engine = sys.modules.get("boardmodeler.pipeline.make_model")
    assert engine is not None, "the sweep's click on GO should have imported the engine"
    assert engine.locate is ltspice.locate, "the engine kept the sandbox's locate()"
    for attribute in ("locate", "locate_outcome", "smoke_test"):
        real = getattr(ltspice, attribute)
        for name, module in sorted(sys.modules.items()):
            if not name.startswith("boardmodeler") or module is ltspice:
                continue
            bound = vars(module).get(attribute)
            assert bound is None or bound is real, f"{name}.{attribute} kept a sandbox stub"


def test_a_surface_that_cannot_be_built_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A surface the sweep cannot open must fail the run, not vanish from the report."""

    def broken() -> Any:
        raise RuntimeError("no window for you")

    monkeypatch.setattr(
        sweep,
        "surface_specs",
        lambda: [sweep.SurfaceSpec(name="DoctorView", factory=broken, reachable="this test")],
    )
    failed = sweep.run_sweep(surfaces=["DoctorView"])
    assert failed.ok is False
    assert [failure["kind"] for failure in failed.failures] == ["surface-construction"]
    # The same decision the command-line entry point makes, so CI sees it as an exit code.
    assert sweep.main(["--surfaces", "DoctorView"]) == 1


@pytest.mark.skipif(
    not os.environ.get("BOARDMODELER_GUI_SWEEP_FULL"),
    reason=(
        "the generous event pump spends a second per click (68 s for all five surfaces, "
        "measured) and would dominate the suite; set BOARDMODELER_GUI_SWEEP_FULL=1 to run it"
    ),
)
def test_the_sweep_is_still_clean_with_a_generous_event_pump(qapp: Any, tmp_path: Path) -> None:
    """Timer- and queue-driven effects need more than three event turns to show up."""
    generous = sweep.run_sweep(sandbox_root=tmp_path, **_FULL_PUMP)
    assert generous.failures == [], generous.failures
    assert generous.totals["clicked"] > 0
    assert generous.totals["unhandled"] == 0
