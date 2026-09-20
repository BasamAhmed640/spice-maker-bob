"""End to end: the engine's real output, judged by real LTspice, shown in the window.

This is the test that decides whether the window is finished. It does not stub the
engine or the simulator: it runs the TPS54320 fixtures through ``make_model`` with the
scripted author (which writes the bundled template library), lets the probe harness
measure the model in real LTspice runs, and then feeds the result object into the window
to prove the table, the status line and the buttons reflect what was actually measured.

It is marked ``gui`` and ``ltspice``: offscreen Qt plus a working simulator.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = [pytest.mark.gui, pytest.mark.ltspice]

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"
DATASHEET = FIXTURES / "originals" / "tps54320_datasheet.pdf"


def test_a_real_build_reaches_the_window(qtbot, tmp_path: Path) -> None:
    from tests.pipeline.test_make_model import make_request

    from boardmodeler.pipeline.make_model import make_model
    from boardmodeler.simulation.ltspice import locate

    install = locate()
    if install is None:
        pytest.skip("LTspice is not installed")

    stages: list[tuple[str, str]] = []
    request = make_request(tmp_path, max_iterations=2, backend_name="scripted", reinforce=False)
    result = make_model(request, progress=lambda event: stages.append((event.stage, event.status)))

    # The engine's own contract, before the window is involved.
    assert result.status == "PASS", result.detail
    assert result.lib_path is not None and result.lib_path.is_file()
    assert result.asy_path is not None and result.asy_path.is_file()
    assert result.card_path is not None and "MODEL_CARD.md" in result.card_path.name
    assert [stage for stage, status in stages if status == "running"] or stages
    assert {"read", "bind", "author", "judge", "save"} <= {stage for stage, _ in stages}

    judged = [row for row in result.rows if row.status == "PASS"]
    not_applicable = [row for row in result.rows if row.status == "NOT_APPLICABLE"]
    assert len(judged) >= 5, f"expected real measured passes, got {result.counts}"
    assert not_applicable, "the fixture's unreachable rows must be declared, not dropped"
    # Every judged row must carry a number the simulator produced.
    for row in judged:
        assert row.measured and row.measured != "-", row

    # ...and now the window, fed the same object the CLI would print.
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    window._on_result(result)

    assert window.rows.rowCount() == len(result.rows)
    shown_statuses = [window.rows.item(r, 3).text() for r in range(window.rows.rowCount())]
    assert shown_statuses.count("PASS") >= 5
    assert "NOT_APPLICABLE" in shown_statuses

    measured_column = [window.rows.item(r, 2).text() for r in range(window.rows.rowCount())]
    assert any("vin_uvlo_rise" in cell or "v_fb" in cell for cell in measured_column), (
        "the window must show the values the simulator measured, not 'ok'"
    )
    assert window.status_label.text().startswith("PASS")
    assert window.install_button.isEnabled() is True
    assert window.again_button.isEnabled() is True
    assert window._out_dir is not None or result.out_dir == tmp_path / "out"
