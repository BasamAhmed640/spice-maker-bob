"""The hourglass: beside the clock, animating during a build, still when nothing runs.

It is drawn with ``QPainter`` in this window on purpose — no GIF or SVG is added under
``installer/assets/``, where another change is rebuilding the payload — so these tests check
the pixels the widget paints itself and the timer that drives them, plus the property the
owner asked for: sand while a build runs, and no spinning when nothing does.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    from boardmodeler.ui.model_maker import ModelMakerWindow

    maker = ModelMakerWindow()
    qtbot.addWidget(maker)
    maker.show()
    qtbot.waitExposed(maker)
    return maker


def _painted_pixels(widget) -> int:
    """How many pixels the widget draws itself, over the window's black background."""
    image = widget.grab().toImage()
    painted = 0
    for x in range(image.width()):
        for y in range(image.height()):
            colour = image.pixelColor(x, y)
            if colour.alpha() and (colour.red(), colour.green(), colour.blue()) != (0, 0, 0):
                painted += 1
    return painted


def _silent_engine(entered: threading.Event, release: threading.Event, calls: list[bool]):
    """An engine that reports nothing until the test releases it, like a slow agent."""

    def make_model(
        request: object,
        progress: object = None,
        cancel: threading.Event | None = None,
    ) -> object:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker was not released")
        calls.append(cancel is not None and cancel.is_set())
        return SimpleNamespace(status="UNKNOWN", detail="", counts={}, rows=())

    return make_model


@pytest.mark.parametrize("ending", ["finish", "cancel"])
def test_the_hourglass_runs_only_while_a_build_runs(qtbot, tmp_path, monkeypatch, ending) -> None:
    from boardmodeler import agent_providers
    from boardmodeler.pipeline import make_model as pipeline
    from boardmodeler.ui import model_maker as ui

    entered, release, cancellations = threading.Event(), threading.Event(), []
    monkeypatch.setattr(pipeline, "make_model", _silent_engine(entered, release, cancellations))
    monkeypatch.setattr(ui, "_agent_availability", lambda: (True, "test agent"))
    monkeypatch.setattr(ui, "_configured_provider", agent_providers.default_provider)

    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    datasheet = tmp_path / "hourglass.pdf"
    datasheet.write_bytes(b"%PDF-1.4")
    window.part_edit.setText("HOURGLASS")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path / "out"))

    hourglass = window.hourglass
    assert hourglass.is_animating() is False, "nothing runs before GO"
    idle = hourglass.grab().toImage()
    qtbot.wait(250)  # several frames' worth of time
    assert hourglass.grab().toImage() == idle, "an idle hourglass must not repaint itself"

    window.go_button.click()
    worker = window._worker
    assert worker is not None, "GO must start a worker"
    try:
        qtbot.waitUntil(entered.is_set, timeout=10_000)
        assert hourglass.is_animating() is True, "GO must start the sand"
        before = hourglass.grab().toImage()
        qtbot.waitUntil(lambda: hourglass.flow > 0.0, timeout=2_000)
        assert hourglass.grab().toImage() != before, "the sand must visibly move"
        if ending == "cancel":
            window.cancel_button.click()
        release.set()
        qtbot.waitUntil(window.go_button.isEnabled, timeout=10_000)
        assert worker.wait(1000)
    finally:
        release.set()
        assert worker.wait(2000)

    assert cancellations == [ending == "cancel"]
    qtbot.waitUntil(lambda: hourglass.is_animating() is False, timeout=2_000)
    settled = hourglass.grab().toImage()
    qtbot.wait(250)
    assert hourglass.grab().toImage() == settled, "the sand stops when the build does"


def test_the_hourglass_sits_beside_the_clock_and_draws_itself(window) -> None:
    row = window.actions_row
    widgets = [row.itemAt(index).widget() for index in range(row.count())]
    assert window.hourglass in widgets and window.elapsed_label in widgets
    assert abs(widgets.index(window.hourglass) - widgets.index(window.elapsed_label)) == 1, (
        "the hourglass belongs immediately beside the timer it animates"
    )
    assert window.hourglass.height() <= window.elapsed_label.height(), (
        "it must stay the height of the timer label, not become a panel of its own"
    )
    assert _painted_pixels(window.hourglass) > 20, (
        "the glass must be drawn by this widget's own painter, not a shipped bitmap"
    )
    assert window.hourglass.toolTip(), "an unexplained icon is a puzzle; it says what it means"


def test_the_hourglass_does_not_start_a_background_repaint(window) -> None:
    """The idle timer must be stopped, not merely invisible: no work while nothing runs."""
    assert window._elapsed_timer.isActive() is False
    assert window.hourglass.is_animating() is False
    window.hourglass.advance()  # one explicit frame is still allowed by hand
    assert window.hourglass.is_animating() is False
    assert window.hourglass.flow > 0.0
