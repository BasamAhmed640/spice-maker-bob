"""Waveform view: a custom ``QPainter`` widget (D12 — no QtCharts).

Traces are plain ``(x, y)`` sequences; the view draws a grid, a legend, a
crosshair readout and vertical violation markers that carry the requirement id
that was violated. Pan/zoom are mouse-driven (wheel zooms around the cursor,
left-drag pans, double-click fits) and also available as methods so tests can
exercise them without synthesising mouse events.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

__all__ = ["TRACE_COLOURS", "Trace", "ViolationMarker", "WaveformView"]

TRACE_COLOURS: tuple[str, ...] = (
    "#55ff55",
    "#55ffff",
    "#ffff55",
    "#ff55ff",
    "#ff5555",
    "#55aaff",
    "#ffffff",
    "#ffaa55",
)

BACKGROUND = QColor("#000000")
PANEL = QColor("#101010")
GRID = QColor("#1e3a1e")
AXIS = QColor("#888888")
TEXT = QColor("#c8c8c8")
MARKER = QColor("#ff5555")

_MAX_POINTS_PER_PIXEL = 2


@dataclass(frozen=True)
class Trace:
    """One signal to draw: sample times (seconds) and values (SI units)."""

    name: str
    x: Sequence[float]
    y: Sequence[float]
    unit: str = ""
    colour: str | None = None


@dataclass(frozen=True)
class ViolationMarker:
    """A requirement violation at a time, linked to its requirement id."""

    req_id: str
    t_s: float


class WaveformView(QWidget):
    """Multi-trace, pan/zoom waveform display with violation markers."""

    marker_clicked = Signal(str, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._traces: list[Trace] = []
        self._markers: list[ViolationMarker] = []
        self._view: tuple[float, float] | None = None
        self._cursor_t: float | None = None
        self._drag_from: tuple[float, float] | None = None
        self._source: Path | None = None
        self.setMinimumSize(320, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------ data

    def set_traces(self, traces: Sequence[Trace]) -> None:
        self._traces = [self._as_trace(trace) for trace in traces]
        self._view = None
        self._cursor_t = None
        self.update()

    def _as_trace(self, trace: Trace) -> Trace:
        return Trace(
            name=str(trace.name),
            x=np.asarray(trace.x, dtype=float),
            y=np.asarray(trace.y, dtype=float),
            unit=str(trace.unit),
            colour=trace.colour,
        )

    def traces(self) -> list[Trace]:
        return list(self._traces)

    def trace_names(self) -> list[str]:
        return [trace.name for trace in self._traces]

    def clear(self) -> None:
        self._traces = []
        self._markers = []
        self._view = None
        self._source = None
        self.update()

    def source_path(self) -> Path | None:
        return self._source

    def add_violation_marker(self, req_id: str, t_s: float) -> None:
        self._markers.append(ViolationMarker(req_id=str(req_id), t_s=float(t_s)))
        self.update()

    def violation_markers(self) -> list[ViolationMarker]:
        return list(self._markers)

    def clear_markers(self) -> None:
        self._markers = []
        self.update()

    def load_raw(self, path: str | Path, *, max_traces: int = 8) -> list[str]:
        """Load traces from an LTspice ``.raw`` via the project's reader."""
        from boardmodeler.simulation.raw import read_raw

        target = Path(path)
        raw = read_raw(target)
        if raw.data.size == 0:
            self.set_traces([])
            self._source = target
            return []
        times = raw.data[:, 0]
        types = raw.variable_types or [""] * len(raw.variables)
        traces: list[Trace] = []
        for index in range(1, min(len(raw.variables), max_traces + 1)):
            traces.append(
                Trace(
                    name=raw.variables[index],
                    x=times,
                    y=raw.data[:, index],
                    unit=types[index] if index < len(types) else "",
                )
            )
        self._source = target
        self.set_traces(traces)
        return self.trace_names()

    # ------------------------------------------------------------------ view

    def view_range(self) -> tuple[float, float] | None:
        if self._view is not None:
            return self._view
        return self._data_range()

    def _data_range(self) -> tuple[float, float] | None:
        starts = [float(np.nanmin(trace.x)) for trace in self._traces if trace.x.size]
        stops = [float(np.nanmax(trace.x)) for trace in self._traces if trace.x.size]
        if not starts or not stops:
            return None
        low, high = min(starts), max(stops)
        if high <= low:
            high = low + 1e-9
        return low, high

    def set_view_range(self, low: float, high: float) -> None:
        if not np.isfinite([low, high]).all() or high <= low:
            return
        self._view = (float(low), float(high))
        self.update()

    def zoom_to_fit(self) -> None:
        self._view = None
        self.update()

    def zoom(self, factor: float, *, centre: float | None = None) -> None:
        """Zoom by ``factor`` (>1 zooms in) around ``centre`` (default: middle)."""
        current = self.view_range()
        if current is None or factor <= 0:
            return
        low, high = current
        pivot = (low + high) / 2.0 if centre is None else float(centre)
        self.set_view_range(pivot - (pivot - low) / factor, pivot + (high - pivot) / factor)

    def pan(self, fraction: float) -> None:
        """Shift the view by ``fraction`` of its width (positive = later)."""
        current = self.view_range()
        if current is None:
            return
        low, high = current
        shift = (high - low) * float(fraction)
        self.set_view_range(low + shift, high + shift)

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), BACKGROUND)
        plot = self._plot_rect()

        if not self._traces or plot.width() <= 2 or plot.height() <= 2:
            self._draw_message(painter, "NO WAVEFORM DATA")
            painter.end()
            return

        low, high = self.view_range() or (0.0, 1.0)
        y_low, y_high = self._y_range(low, high)
        self._draw_grid(painter, plot, low, high, y_low, y_high)
        self._draw_traces(painter, plot, low, high, y_low, y_high)
        self._draw_markers(painter, plot, low, high)
        self._draw_legend(painter, plot)
        self._draw_readout(painter, plot, low, high, y_low, y_high)
        painter.end()

    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(56.0, 10.0, -10.0, -26.0)

    def _y_range(self, low: float, high: float) -> tuple[float, float]:
        values: list[np.ndarray] = []
        for trace in self._traces:
            if trace.x.size == 0:
                continue
            mask = (trace.x >= low) & (trace.x <= high)
            if mask.any():
                window = trace.y[mask]
                window = window[np.isfinite(window)]
                if window.size:
                    values.append(window)
        if not values:
            return 0.0, 1.0
        combined = np.concatenate(values)
        y_low = float(np.min(combined))
        y_high = float(np.max(combined))
        span = y_high - y_low
        pad = max(abs(y_high) * 0.1, 1e-6) if span <= 0 else span * 0.08
        return y_low - pad, y_high + pad

    def _to_point(
        self,
        t: float,
        value: float,
        plot: QRectF,
        low: float,
        high: float,
        y_low: float,
        y_high: float,
    ) -> QPointF:
        x = plot.left() + (t - low) / (high - low) * plot.width()
        y = plot.bottom() - (value - y_low) / (y_high - y_low) * plot.height()
        return QPointF(x, y)

    def _draw_grid(
        self,
        painter: QPainter,
        plot: QRectF,
        low: float,
        high: float,
        y_low: float,
        y_high: float,
    ) -> None:
        painter.fillRect(plot, PANEL)
        painter.setPen(QPen(GRID, 1))
        for division in range(1, 10):
            x = plot.left() + plot.width() * division / 10.0
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            y = plot.top() + plot.height() * division / 10.0
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(AXIS, 1))
        painter.drawRect(plot)
        painter.setFont(self._label_font())
        painter.setPen(TEXT)
        painter.drawText(QPointF(4.0, plot.top() + 12.0), f"{y_high:.3g}")
        painter.drawText(QPointF(4.0, plot.bottom()), f"{y_low:.3g}")
        painter.drawText(QPointF(plot.left(), plot.bottom() + 18.0), f"{low:.4g} s")
        painter.drawText(QPointF(plot.right() - 60.0, plot.bottom() + 18.0), f"{high:.4g} s")

    def _draw_traces(
        self,
        painter: QPainter,
        plot: QRectF,
        low: float,
        high: float,
        y_low: float,
        y_high: float,
    ) -> None:
        width = max(int(plot.width()), 1)
        for index, trace in enumerate(self._traces):
            if trace.x.size == 0:
                continue
            colour = QColor(trace.colour or TRACE_COLOURS[index % len(TRACE_COLOURS)])
            points = self._visible_points(trace, low, high, width)
            if len(points) < 2:
                continue
            painter.setPen(QPen(colour, 1))
            polygon = [
                self._to_point(t, value, plot, low, high, y_low, y_high) for t, value in points
            ]
            painter.drawPolyline(polygon)

    def _visible_points(
        self, trace: Trace, low: float, high: float, width: int
    ) -> list[tuple[float, float]]:
        x = trace.x
        y = trace.y
        left = int(np.searchsorted(x, low, side="left"))
        right = int(np.searchsorted(x, high, side="right"))
        left = max(0, min(left, x.size - 1))
        right = max(left + 1, min(right, x.size))
        xs = x[left:right]
        ys = y[left:right]
        if xs.size <= width * _MAX_POINTS_PER_PIXEL:
            return [(float(a), float(b)) for a, b in zip(xs, ys, strict=True)]

        edges = np.linspace(float(xs[0]), float(xs[-1]), width + 1)
        bins = np.clip(np.digitize(xs, edges) - 1, 0, width - 1)
        y_min = np.full(width, np.nan)
        y_max = np.full(width, np.nan)
        np.minimum.at(y_min, bins, ys)
        np.maximum.at(y_max, bins, ys)
        centres = (edges[:-1] + edges[1:]) / 2.0
        points: list[tuple[float, float]] = []
        for column in range(width):
            if np.isnan(y_min[column]):
                continue
            points.append((float(centres[column]), float(y_min[column])))
            if y_max[column] != y_min[column]:
                points.append((float(centres[column]), float(y_max[column])))
        return points

    def _draw_markers(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        painter.setFont(self._label_font())
        for marker in self._markers:
            if not (low <= marker.t_s <= high):
                continue
            x = plot.left() + (marker.t_s - low) / (high - low) * plot.width()
            painter.setPen(QPen(MARKER, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(MARKER)
            painter.drawText(QPointF(x + 3.0, plot.top() + 12.0), marker.req_id)

    def _draw_legend(self, painter: QPainter, plot: QRectF) -> None:
        painter.setFont(self._label_font())
        x = plot.left() + 8.0
        y = plot.top() + 14.0
        for index, trace in enumerate(self._traces):
            colour = QColor(trace.colour or TRACE_COLOURS[index % len(TRACE_COLOURS)])
            painter.setPen(QPen(colour, 2))
            painter.drawLine(QPointF(x, y - 4.0), QPointF(x + 18.0, y - 4.0))
            painter.setPen(TEXT)
            label = trace.name + (f" [{trace.unit}]" if trace.unit else "")
            painter.drawText(QPointF(x + 24.0, y), label)
            y += 14.0

    def _draw_readout(
        self,
        painter: QPainter,
        plot: QRectF,
        low: float,
        high: float,
        y_low: float,
        y_high: float,
    ) -> None:
        if self._cursor_t is None:
            return
        x = plot.left() + (self._cursor_t - low) / (high - low) * plot.width()
        painter.setPen(QPen(AXIS, 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        painter.setFont(self._label_font())
        painter.setPen(TEXT)
        lines = [f"t = {self._cursor_t:.6g} s"]
        for trace in self._traces:
            if trace.x.size == 0:
                continue
            index = int(np.searchsorted(trace.x, self._cursor_t))
            index = max(0, min(index, trace.x.size - 1))
            lines.append(f"{trace.name} = {float(trace.y[index]):.5g}")
        box = QRectF(plot.right() - 170.0, plot.top() + 4.0, 166.0, 15.0 * len(lines) + 6.0)
        painter.fillRect(box, PANEL)
        painter.setPen(QPen(AXIS, 1))
        painter.drawRect(box)
        painter.setPen(TEXT)
        for row, text in enumerate(lines):
            painter.drawText(QPointF(box.left() + 4.0, box.top() + 14.0 + 15.0 * row), text)

    def _draw_message(self, painter: QPainter, message: str) -> None:
        painter.setPen(TEXT)
        painter.setFont(self._label_font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)

    def _label_font(self) -> QFont:
        font = QFont("Consolas", 8)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        return font

    # ------------------------------------------------------------------ mouse

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = (event.position().x(), event.position().y())
        elif event.button() == Qt.MouseButton.RightButton:
            self.zoom_to_fit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        current = self.view_range()
        if current is None:
            return
        low, high = current
        plot = self._plot_rect()
        fraction = (event.position().x() - plot.left()) / max(plot.width(), 1.0)
        self._cursor_t = low + fraction * (high - low)
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta_px = event.position().x() - self._drag_from[0]
            self.pan(-delta_px / max(plot.width(), 1.0))
            self._drag_from = (event.position().x(), event.position().y())
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = None
            if self._cursor_t is not None:
                for marker in self._markers:
                    if abs(marker.t_s - self._cursor_t) < 1e-9:
                        self.marker_clicked.emit(marker.req_id, marker.t_s)

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:
        self.zoom_to_fit()

    def leaveEvent(self, _event: object) -> None:
        self._cursor_t = None
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        self.zoom(1.25**steps, centre=self._cursor_t)
