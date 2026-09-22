"""End to end: the engine's real output, judged by real LTspice, shown in the window.

This is the test that decides whether the window is finished. It does not stub the
engine or the simulator: it runs the TPS54320 fixtures through ``make_model`` with the
scripted author (which writes the bundled template library), lets the probe harness
measure the model in real LTspice runs, and then feeds the result object into the window
to prove the table, the status line and the buttons reflect what was actually measured.

It is marked ``gui`` and ``ltspice``: offscreen Qt plus a working simulator.

The verdict this build earns is ``UNKNOWN`` with limited verified coverage, not
``PASS``: nine bound rows are measured in real LTspice runs, and the remaining
datasheet rows are UNKNOWN because the reduced template cannot expose them. That
is the honest product answer ("a model, with limited verified coverage") and it is
what the window must show.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from boardmodeler.ui.model_maker import ModelMakerWindow

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = [pytest.mark.gui, pytest.mark.ltspice]

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"
DATASHEET = FIXTURES / "originals" / "tps54320_datasheet.pdf"


def _cell(window: ModelMakerWindow, row: int, column: int) -> str:
    """The text the window's table shows at ``(row, column)``."""
    item = window.rows.item(row, column)
    assert item is not None, f"the table has no cell at ({row}, {column})"
    return item.text()


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
    #
    # UNKNOWN is the correct verdict here and must not be "fixed" back to PASS.
    # The reviewed binding (fixtures/regulator/tps54320/probes.json) declares nine
    # probed rows out of 38 and a written not_testable_reason for the other 29, and
    # the bundled template is a reduced model whose ports are VIN, EN, FB, PG, VOUT,
    # GND, SW and ILIM_MODE -- no PVIN, BOOT, PH, COMP or SS/TR. `_Run.decide()`
    # returns PASS only when *no* row is UNKNOWN (pipeline/make_model.py), and rows
    # stating a numeric datasheet limit without a measurement are preserved as
    # UNKNOWN on purpose ("Preserve untested quantitative rows as UNKNOWN",
    # docs/DECISIONS.md, 2026-09-20, commit cc7c558). A PASS here would claim
    # coverage the simulator run did not produce. tests/pipeline/test_make_model.py
    # pins the same split without the window.
    assert result.status == "UNKNOWN", result.detail
    assert "limited coverage" in result.detail
    assert result.counts.get("FAIL", 0) == 0, result.counts
    assert result.lib_path is not None and result.lib_path.is_file()
    assert result.asy_path is not None and result.asy_path.is_file()
    assert result.card_path is not None and "MODEL_CARD.md" in result.card_path.name
    assert [stage for stage, status in stages if status == "running"] or stages
    assert {"read", "bind", "author", "judge", "save"} <= {stage for stage, _ in stages}

    judged = [row for row in result.rows if row.status == "PASS"]
    unknown = [row for row in result.rows if row.status == "UNKNOWN"]
    not_applicable = [row for row in result.rows if row.status == "NOT_APPLICABLE"]
    # The reviewed fixture's split, pinned identically in tests/pipeline/test_make_model.py:
    # nine rows judged by eight probes, 21 quantitative rows the reduced model cannot
    # expose, 8 rows that declare no numeric limit. A silently dropped probe moves a row
    # between these groups here, so real measured coverage cannot shrink unnoticed.
    assert len(judged) == 9, f"expected the fixture's nine bound rows, got {result.counts}"
    assert len(unknown) == 21, f"expected 21 unmeasured numeric rows, got {result.counts}"
    assert len(not_applicable) == 8, result.counts
    assert len(judged) + len(unknown) + len(not_applicable) == len(result.rows)
    assert not_applicable, "the fixture's unreachable rows must be declared, not dropped"
    # Nothing that was not measured may carry a value, and every gap says why.
    for row in unknown + not_applicable:
        assert row.required.strip() and row.required != "no simulation probe", row
        assert row.measured == "-", row
    # Every judged row must carry a number the simulator produced.
    for row in judged:
        assert row.measured and row.measured != "-", row

    # ...and now the window, fed the same object the CLI would print.
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    window._on_result(result)

    assert window.rows.rowCount() == len(result.rows)
    shown_statuses = [_cell(window, r, 3) for r in range(window.rows.rowCount())]
    assert shown_statuses.count("PASS") == len(judged)
    assert shown_statuses.count("UNKNOWN") == len(unknown)
    assert "NOT_APPLICABLE" in shown_statuses
    # An UNKNOWN row must show the reason it lacks a measurement for (the "Required"
    # column carries the binding's written reason), so the table never reads as a
    # silent gap the operator would mistake for coverage.
    for index, status in enumerate(shown_statuses):
        if status == "UNKNOWN":
            reason = _cell(window, index, 1)
            assert reason.strip(), f"row {index} is UNKNOWN with no reason"

    measured_column = [_cell(window, r, 2) for r in range(window.rows.rowCount())]
    assert any("vin_at_start" in cell or "v_fb" in cell for cell in measured_column), (
        "the window must show the values the simulator measured, not 'ok'"
    )
    # The status line states what the build actually is: limited verified coverage.
    assert window.status_label.text().startswith("UNKNOWN")
    assert "limited coverage" in window.status_label.toolTip()
    assert window.install_button.isEnabled() is True
    assert window.again_button.isEnabled() is True
    assert window._out_dir is not None or result.out_dir == tmp_path / "out"
