"""Waveform view tests: traces, pan/zoom, markers, painting, .raw loading."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QImage

from boardmodeler.ui.waveforms import Trace, WaveformView

pytestmark = pytest.mark.gui


def _divider_traces() -> list[Trace]:
    t = np.linspace(0.0, 1e-3, 101)
    return [
        Trace(name="V(in)", x=t, y=np.full_like(t, 5.0), unit="voltage"),
        Trace(name="V(out)", x=t, y=5.0 * (1.0 - np.exp(-t / 2e-4)), unit="voltage"),
    ]


def _colours(image: QImage) -> set[int]:
    colours: set[int] = set()
    for y in range(0, image.height(), 3):
        for x in range(0, image.width(), 3):
            colours.add(image.pixel(x, y))
    return colours


@pytest.fixture
def view(qapp) -> WaveformView:
    widget = WaveformView()
    widget.resize(480, 320)
    return widget


def test_set_traces_exposes_names_and_range(view: WaveformView) -> None:
    view.set_traces(_divider_traces())
    assert view.trace_names() == ["V(in)", "V(out)"]
    low, high = view.view_range()
    assert low == pytest.approx(0.0)
    assert high == pytest.approx(1e-3)


def test_zoom_and_pan_change_the_view_range(view: WaveformView) -> None:
    view.set_traces(_divider_traces())
    view.zoom(2.0)
    low, high = view.view_range()
    assert high - low == pytest.approx(0.5e-3)

    # zooming about a point outside the view still shrinks the span by 2
    view.zoom(2.0, centre=1e-3)
    low, high = view.view_range()
    assert high - low == pytest.approx(0.25e-3)
    assert low > 0.5e-3

    before = view.view_range()
    view.pan(0.5)
    after = view.view_range()
    assert after[0] - before[0] == pytest.approx((before[1] - before[0]) * 0.5)

    view.zoom_to_fit()
    assert view.view_range()[1] == pytest.approx(1e-3)


def test_view_range_is_none_without_traces(view: WaveformView) -> None:
    assert view.view_range() is None
    assert view.trace_names() == []


def test_violation_markers_are_linked_to_requirement_ids(view: WaveformView) -> None:
    view.add_violation_marker("REQ_PG_001", 2.5e-4)
    view.add_violation_marker("REQ_UVLO", 7.5e-4)
    markers = view.violation_markers()
    assert [(m.req_id, m.t_s) for m in markers] == [
        ("REQ_PG_001", 2.5e-4),
        ("REQ_UVLO", 7.5e-4),
    ]
    view.clear_markers()
    assert view.violation_markers() == []


def test_painting_draws_a_limited_palette(view: WaveformView) -> None:
    view.set_traces(_divider_traces())
    view.add_violation_marker("REQ_PG_001", 5e-4)
    pixmap = view.grab()
    image = pixmap.toImage()
    assert (image.width(), image.height()) == (480, 320)
    colours = _colours(image)
    # a drawn grid, two traces, a marker and text: more than the flat background,
    # but still a deliberately limited 8-bit-style palette
    assert 3 < len(colours) <= 40


def test_empty_view_paints_the_placeholder(view: WaveformView) -> None:
    image = view.grab().toImage()
    assert len(_colours(image)) >= 2


def test_load_raw_reads_variables_and_time_axis(tmp_path: Path, view: WaveformView) -> None:
    raw = tmp_path / "probe.raw"
    raw.write_text(
        "Title: * synthetic probe\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        "No. Variables: 3\n"
        "No. Points: 3\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tV(out)\tvoltage\n"
        "\t2\tI(R1)\tdevice_current\n"
        "Values:\n"
        "0\t0.0\t0.0\t0.0\n"
        "1\t1e-3\t0.632\t0.000632\n"
        "2\t2e-3\t0.865\t0.000865\n",
        encoding="utf-8",
    )
    names = view.load_raw(raw)
    assert names == ["V(out)", "I(R1)"]
    assert view.source_path() == raw
    assert view.traces()[0].unit == "voltage"
    low, high = view.view_range()
    assert (low, high) == (0.0, 2e-3)


def test_clear_removes_traces_and_markers(view: WaveformView) -> None:
    view.set_traces(_divider_traces())
    view.add_violation_marker("REQ", 1e-4)
    view.clear()
    assert view.trace_names() == []
    assert view.violation_markers() == []
    assert view.source_path() is None
