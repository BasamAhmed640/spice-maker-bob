"""The model maker: part number + datasheet + save location in, judged model out.

This window is the product and it holds only what a build needs: which part, which
datasheet, where the model goes, a GO button, and the progress of the run. Settings that
persist between sessions — the LTspice path, the agent's API key, the default model
folder, the LTspice library location, the web-reinforcement switch — live in
:mod:`boardmodeler.ui.setup_dialog`, reached from the SETUP button.

The engine runs in a worker thread and reports the same stages the CLI prints, so the two
surfaces cannot drift apart. Nothing here computes a verdict.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from boardmodeler.agent_providers import AgentProvider
from boardmodeler.storage import local_path, model_dir, portable
from boardmodeler.ui.file_dialogs import starting_directory
from boardmodeler.ui.theme import CGA, RETRO_STYLESHEET

__all__ = ["DoctorView", "HourglassWidget", "ModelMakerWindow", "readable_doctor_report"]

_STATUS_COLOUR = {
    "PASS": "#55ff55",
    "FAIL": "#ff5555",
    "UNKNOWN": "#ffff55",
    "BLOCKED": "#ff55ff",
    "NOT_APPLICABLE": "#5555ff",
    "running": "#55ffff",
    "ok": "#55ff55",
    "failed": "#ff5555",
    "skipped": "#aaaaaa",
}


class MakeModelWorker(QThread):
    """Runs ``make_model`` off the UI thread; cancellation reaches the agent process."""

    progressed = Signal(object)
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, request: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._request = request
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # Qt entry point
        try:
            from boardmodeler.pipeline.make_model import make_model

            result = make_model(
                self._request,  # type: ignore[arg-type]
                progress=self.progressed.emit,
                cancel=self._cancel,
            )
        except Exception as exc:  # reported to the user, never a traceback in a slot
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_result.emit(result)


def _window_title() -> str:
    """The build's own name: a restricted catalog says whose build this is (D-015)."""
    from boardmodeler import agent_providers

    only = agent_providers.only_provider()
    suffix = f" · {only.label} only" if only is not None else ""
    from boardmodeler import __version__

    return f"Spice Maker {__version__} — IC model maker{suffix}"


def _configured_provider() -> AgentProvider | None:
    """The accepted provider the persisted settings name, or ``None`` — never a substitute."""
    try:
        from boardmodeler.config import load_config
        from boardmodeler.ui.setup_dialog import configured_provider

        provider, _ = configured_provider(load_config())
        return provider
    except Exception:  # an unreadable config is reported by the availability check
        return None


def _configured_provider_id() -> str:
    """The provider id exactly as the config names it: the request carries it unsubstituted.

    ``build_api_backend`` refuses an id this build does not accept with
    ``api_provider_unavailable: ...``, so passing the raw value is what makes the
    engine's own refusal reachable instead of another provider running with the
    wrong key.
    """
    from boardmodeler.config import load_config

    return str(load_config().agent_provider or "").strip()


def _agent_availability() -> tuple[bool, str]:
    """Can the agent run at all? Checked before a long run instead of after it fails."""
    try:
        from boardmodeler.authoring.api_backend import build_api_backend

        return build_api_backend(_configured_provider_id() or None).availability()
    except Exception as exc:  # pragma: no cover - import/config problems are reported
        return False, f"the agent backend could not be loaded: {exc}"


def _colour(value: str) -> QColor:
    return QColor(value)


class HourglassWidget(QWidget):
    """A small line-art hourglass that animates only while a build runs.

    Drawn with ``QPainter`` rather than shipped as a GIF or SVG: the installer payload is
    rebuilt elsewhere, so a binary asset here would collide with that work. It is cheap by
    construction — an 18x22 box, a dozen pen strokes, and a timer that exists only between
    :meth:`start` and :meth:`stop` — so nothing repaints in the background while the
    application sits idle.
    """

    #: ~12 frames per second: motion the eye reads, at a repaint cost of one small widget.
    INTERVAL_MS = 80
    #: Frames of sand the top bulb empties over; the drain restarts when it is over.
    FRAMES_PER_DRAIN = 24

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flow = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.advance)
        self.setFixedSize(18, 22)
        self.setToolTip("The sand runs for as long as this build does")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------ state
    def start(self) -> None:
        """Start the drain, from a full top bulb; running twice is a no-op."""
        if self._timer.isActive():
            return
        self._flow = 0.0
        self._timer.start()
        self.update()

    def stop(self) -> None:
        """Settle the sand; after this the widget repaints only when told to."""
        if not self._timer.isActive():
            return
        self._timer.stop()
        self.update()

    def is_animating(self) -> bool:
        """True only while a build runs: the one time this widget drives its own repaint."""
        return self._timer.isActive()

    @property
    def flow(self) -> float:
        """How much sand has left the top bulb, 0.0 (full) to 1.0 (drained)."""
        return self._flow

    def advance(self) -> None:
        """One frame of falling sand — the timer slot, and how tests step it deterministically."""
        self._flow = (self._flow + 1.0 / self.FRAMES_PER_DRAIN) % 1.0
        self.update()

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event: object) -> None:  # Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        running = self._timer.isActive()
        flow = self._flow if running else 1.0  # at rest the sand has settled at the bottom

        top_bulb = QPainterPath()
        top_bulb.moveTo(4.0, 2.5)
        top_bulb.lineTo(14.0, 2.5)
        top_bulb.lineTo(9.0, 11.0)
        top_bulb.closeSubpath()
        bottom_bulb = QPainterPath()
        bottom_bulb.moveTo(9.0, 11.0)
        bottom_bulb.lineTo(14.0, 19.5)
        bottom_bulb.lineTo(4.0, 19.5)
        bottom_bulb.closeSubpath()

        glass = QPen(QColor(CGA["bright_cyan"]))
        glass.setWidthF(1.0)
        painter.setPen(glass)
        painter.drawLine(QPointF(2.5, 2.0), QPointF(15.5, 2.0))
        painter.drawLine(QPointF(2.5, 20.0), QPointF(15.5, 20.0))
        painter.drawPolyline(QPolygonF([QPointF(4.0, 3.0), QPointF(9.0, 11.0), QPointF(4.0, 19.0)]))
        painter.drawPolyline(
            QPolygonF([QPointF(14.0, 3.0), QPointF(9.0, 11.0), QPointF(14.0, 19.0)])
        )

        sand = QColor(CGA["bright_cyan"])
        sand.setAlpha(190)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(sand))
        surface = 2.5 + (11.0 - 2.5) * flow
        if surface < 11.0:
            painter.save()
            painter.setClipPath(top_bulb)
            painter.drawRect(QRectF(3.0, surface, 12.0, 11.0 - surface))
            painter.restore()
        pile = 19.5 - (19.5 - 11.0) * flow
        if pile < 19.5:
            painter.save()
            painter.setClipPath(bottom_bulb)
            painter.drawRect(QRectF(3.0, pile, 12.0, 19.6 - pile))
            painter.restore()
        if running:  # the thread of sand, from the neck down to the top of the pile
            painter.setPen(QPen(sand, 1.0))
            painter.drawLine(QPointF(9.0, 11.0), QPointF(9.0, pile))
        painter.end()


def readable_doctor_report(raw: str) -> str:
    """The report the CLI renders for a person, falling back to the JSON it was given.

    ``doctor --json`` is the invocation both surfaces share, so the window asks the CLI
    to render the same payload the command line would print — the two cannot drift.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(payload, dict):
        return raw
    try:
        from boardmodeler.cli import _render_doctor_human

        return _render_doctor_human(payload)
    except Exception:  # pragma: no cover - a report must survive a renamed renderer
        return json.dumps(payload, indent=2, sort_keys=True)


class DoctorView(QDialog):
    """The whole environment report on a page that can be read, resized and copied.

    The earlier surface was a ``QMessageBox`` showing ``report[-4000:]``: the head of the
    report (version, config path, LTspice) was cut off, and a message box gives no more
    than a few lines at a time. This page holds the report in full in a read-only
    monospace view — the readable rendering first, the raw JSON one click away — sized to
    a modest default and resizable, with COPY REPORT for pasting into a bug report.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("boardmodeler doctor")
        self.setStyleSheet(RETRO_STYLESHEET)
        self._raw = ""
        self._readable = ""
        self._showing_raw = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet(
            f"color: {CGA['bright_green']}; font-family: Consolas; font-size: 9pt;"
        )
        layout.addWidget(self.summary_label)
        self.report_view = QPlainTextEdit()
        self.report_view.setObjectName("doctorReport")
        self.report_view.setReadOnly(True)
        self.report_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.report_view.setToolTip("The doctor report, in full")
        layout.addWidget(self.report_view, 1)

        row = QHBoxLayout()
        self.copy_button = QPushButton("COPY REPORT")
        self.copy_button.setToolTip("Put the report on the clipboard exactly as shown")
        self.copy_button.clicked.connect(self.copy_report)
        self.raw_button = QPushButton("SHOW RAW JSON")
        self.raw_button.clicked.connect(self.toggle_raw)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.close)
        row.addWidget(self.copy_button)
        row.addWidget(self.raw_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        # A modest default — the whole point is that the user can drag or maximise it,
        # which the message box this replaced could not do at all.
        self.resize(760, 460)
        self.setMinimumSize(420, 240)

    # ------------------------------------------------------------------ report
    def set_report(self, raw: str, *, exit_code: int = 0) -> None:
        """Take the whole report; nothing is trimmed on the way in."""
        from PySide6.QtGui import QFontDatabase

        self._raw = raw
        self._readable = readable_doctor_report(raw)
        self.summary_label.setText(
            f"doctor exit code {exit_code} · {len(raw)} characters, shown in full"
        )
        self.report_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.show_readable()

    @property
    def raw_json(self) -> str:
        """The payload exactly as the CLI printed it, complete."""
        return self._raw

    @property
    def readable_report(self) -> str:
        """The same payload rendered for a person."""
        return self._readable

    @property
    def report_text(self) -> str:
        """The text currently on the page, whole."""
        return self.report_view.toPlainText()

    def show_readable(self) -> None:
        self._showing_raw = False
        self.raw_button.setText("SHOW RAW JSON")
        self.report_view.setPlainText(self._readable)

    def show_raw(self) -> None:
        self._showing_raw = True
        self.raw_button.setText("SHOW READABLE REPORT")
        self.report_view.setPlainText(self._raw)

    def toggle_raw(self) -> None:
        self.show_readable() if self._showing_raw else self.show_raw()

    def copy_report(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:  # missing only when no application object exists
            clipboard.setText(self.report_view.toPlainText())


class ModelMakerWindow(QMainWindow):
    """Part number + datasheet + save location -> agent-authored, simulator-judged model."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_window_title())
        # The build window is resizable: 900x600 is where it opens, not a cage. The floor is
        # the content's own minimum size, computed once the layout is built, so shrinking it
        # can never hide a control; maximising is the user's to do.
        self.resize(900, 600)
        self._worker: MakeModelWorker | None = None
        self._result: object | None = None
        self._out_dir: Path | None = None
        self.doctor_view: DoctorView | None = None
        self._started_at: float | None = None
        self._elapsed_seconds = 0.0
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        self.setStyleSheet(_window_stylesheet())
        central = QWidget(self)
        central.setObjectName("root")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        layout.addLayout(self._build_top_row())
        layout.addLayout(self._build_inputs())
        layout.addLayout(self._build_actions())
        layout.addWidget(QLabel("PROGRESS"))
        layout.addWidget(self._build_stages(), 2)
        layout.addWidget(self._build_result_header())
        layout.addWidget(self._build_rows(), 5)
        layout.addLayout(self._build_result_actions())
        layout.activate()
        self.setMinimumSize(_smallest_useful(layout.minimumSize()))

    # ------------------------------------------------------------------ widgets
    def _build_top_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        setup = QPushButton("SETUP")
        setup.setToolTip("LTspice path, agent key, model folder, web reinforcement")
        setup.clicked.connect(self._open_setup)
        check = QPushButton("CHECK ENVIRONMENT")
        check.setToolTip("Where LTspice, the reader backend and the agent key stand")
        check.clicked.connect(self._run_doctor)
        row.addWidget(setup)
        row.addWidget(check)
        row.addStretch(1)
        self.setup_hint = QLabel("")
        self.setup_hint.setStyleSheet("color: #ff5555; font-family: Consolas;")
        row.addWidget(self.setup_hint)
        return row

    def _build_inputs(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)

        self.part_edit = QLineEdit()
        self.part_edit.setPlaceholderText("TPS54320")
        grid.addWidget(QLabel("PART NUMBER"), 0, 0)
        grid.addWidget(self.part_edit, 0, 1, 1, 2)

        self.datasheet_edit = QLineEdit()
        self.datasheet_edit.setPlaceholderText("the manufacturer datasheet (PDF)")
        browse_pdf = QPushButton("Choose PDF…")
        browse_pdf.clicked.connect(self._choose_datasheet)
        grid.addWidget(QLabel("DATASHEET"), 1, 0)
        grid.addWidget(self.datasheet_edit, 1, 1)
        grid.addWidget(browse_pdf, 1, 2)

        self.out_edit = QLineEdit(str(_default_model_dir()))
        browse_out = QPushButton("Choose folder…")
        browse_out.clicked.connect(self._choose_out)
        grid.addWidget(QLabel("SAVE MODEL TO"), 2, 0)
        grid.addWidget(self.out_edit, 2, 1)
        grid.addWidget(browse_out, 2, 2)
        return grid

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.go_button = QPushButton("GO")
        self.go_button.setToolTip(
            "Send this datasheet and model text to the provider selected in SETUP"
        )
        self.go_button.clicked.connect(self._make_model)
        self.cancel_button = QPushButton("CANCEL")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.go_button, 3)
        row.addWidget(self.cancel_button, 1)
        # The hourglass sits immediately beside the clock it belongs to, and both are driven
        # by the same two places: _set_busy starts them on GO and stops them on every exit.
        self.hourglass = HourglassWidget()
        self.hourglass.setObjectName("hourglass")
        row.addWidget(self.hourglass)
        self.elapsed_label = QLabel("ELAPSED 00:00:00")
        self.elapsed_label.setStyleSheet("color: #55ffff; font-family: Consolas;")
        self.elapsed_label.setToolTip(
            "Time since this build started (hours:minutes:seconds). "
            "Includes waiting for the agent and cancellation; not an estimate of time remaining."
        )
        row.addWidget(self.elapsed_label)
        self.actions_row = row
        return row

    def _build_stages(self) -> QTableWidget:
        self.stages = QTableWidget(0, 3)
        self.stages.setHorizontalHeaderLabels(["Stage", "Status", "Detail"])
        self.stages.verticalHeader().setVisible(False)
        self.stages.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stages.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.stages.setMaximumHeight(120)
        header = self.stages.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        return self.stages

    def _build_result_header(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel(
            "GO sends this datasheet and model text to the provider selected in SETUP"
        )
        # Wrapped, not clipped: a status line can carry a whole failure reason, and an
        # unwrapped QLabel would force the window's minimum width to the full sentence.
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #ffffff; font-family: Consolas; font-size: 10pt;")
        row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(140)
        row.addWidget(self.progress)
        return holder

    def _build_rows(self) -> QTableWidget:
        self.rows = QTableWidget(0, 5)
        self.rows.setHorizontalHeaderLabels(
            ["Requirement", "Required", "Measured", "Status", "Page"]
        )
        self.rows.verticalHeader().setVisible(False)
        self.rows.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.rows.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        return self.rows

    def _build_result_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.open_button = QPushButton("Open model folder")
        self.open_button.clicked.connect(self._open_folder)
        self.install_button = QPushButton("Install into LTspice")
        self.install_button.clicked.connect(self._install)
        for button in (self.open_button, self.install_button):
            button.setEnabled(False)
            row.addWidget(button)
        row.addStretch(1)
        self.again_button = QPushButton("Run tests again")
        self.again_button.clicked.connect(self._rerun_tests)
        self.again_button.setEnabled(False)
        row.addWidget(self.again_button)
        return row

    # ------------------------------------------------------------------ helpers
    def _choose_datasheet(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose the datasheet",
            # A folder, always: the field holds a PDF path, and Qt's third argument is
            # where the dialog opens, so a PDF (or a stale one) would fall back to C:\\.
            starting_directory(self.datasheet_edit.text()),
            "PDF (*.pdf)",
        )
        if path:
            self.datasheet_edit.setText(path)
            if not self.part_edit.text().strip():
                self.part_edit.setText(Path(path).stem.split("_")[0].upper())

    def _choose_out(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Where should the model be saved?",
            # Same rule: a configured folder can point at a file, or at nothing at all.
            starting_directory(self.out_edit.text()),
        )
        if path:
            self.out_edit.setText(path)

    def _set_busy(self, busy: bool) -> None:
        self.go_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.again_button.setEnabled(not busy and self._result is not None)
        self.progress.setVisible(busy)
        if busy:
            self._started_at = time.monotonic()
            self._elapsed_seconds = 0.0
            self._elapsed_timer.start()
            self.hourglass.start()
            self.stages.setRowCount(0)
            self.rows.setRowCount(0)
            self._result = None
            self.install_button.setEnabled(False)
            self.open_button.setEnabled(False)
        else:
            if self._started_at is not None:
                self._elapsed_seconds = time.monotonic() - self._started_at
                self._started_at = None
            self._elapsed_timer.stop()
            self.hourglass.stop()
        self._update_elapsed()

    def _elapsed(self) -> str:
        seconds = self._elapsed_seconds
        if self._started_at is not None:
            seconds = time.monotonic() - self._started_at
        hours, remainder = divmod(max(0, math.floor(seconds)), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _update_elapsed(self) -> None:
        self.elapsed_label.setText(f"ELAPSED {self._elapsed()}")

    # ------------------------------------------------------------------ actions
    def _make_model(self) -> None:
        part = self.part_edit.text().strip()
        datasheet = Path(self.datasheet_edit.text().strip())
        out_dir = Path(self.out_edit.text().strip() or _default_model_dir())
        try:
            out_dir = local_path(out_dir)
        except ValueError as exc:
            QMessageBox.warning(self, "Choose a local folder", str(exc))
            return
        if not part:
            QMessageBox.warning(self, "Part number needed", "Which part should be modelled?")
            return
        if not datasheet.is_file():
            QMessageBox.warning(self, "Datasheet needed", f"Not a readable file: {datasheet}")
            return

        try:
            from boardmodeler.pipeline.make_model import MakeModelRequest
        except ImportError as exc:  # pragma: no cover - only while the engine is absent
            QMessageBox.warning(
                self,
                "Engine not available",
                "The model-making engine is not installed in this build:\n\n"
                f"{exc}\n\nUpdate the checkout (git pull) and try again.",
            )
            return

        provider = _configured_provider()
        usable, reason = _agent_availability()
        if not usable:
            self.setup_hint.setText("no agent key — press SETUP")
            if provider is None:
                advice = "Press SETUP, select USE IBM BOB, then SAVE."
            else:
                advice = (
                    f"Press SETUP and store your {provider.label} API key ({provider.key_hint})."
                )
            QMessageBox.warning(
                self,
                "No agent available",
                "Nothing was started, because the agent that writes the model is not usable "
                f"yet:\n\n{reason}\n\n{advice}",
            )
            return
        self.setup_hint.setText("")
        out_dir.mkdir(parents=True, exist_ok=True)

        subckt = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in part).upper()
        from boardmodeler.config import load_config

        request = MakeModelRequest(
            part=part,
            subckt=subckt,
            datasheet=datasheet,
            out_dir=out_dir,
            backend_name="api",
            verification="full" if load_config().full_verification else "sanity",
            allow_remote=True,
            # The configured id as written, so ``build_api_backend`` refuses a provider
            # this build lacks instead of another provider answering with the wrong key.
            provider=provider.id if provider is not None else _configured_provider_id(),
        )
        self._out_dir = out_dir
        self._start(request)

    def _start(self, request: object) -> None:
        self._set_busy(True)
        self.status_label.setText("working…")
        self.status_label.setStyleSheet("color: #55ffff; font-family: Consolas; font-size: 10pt;")
        worker = MakeModelWorker(request, self)
        worker.progressed.connect(self._on_stage)
        worker.finished_result.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _rerun_tests(self) -> None:
        if self._out_dir is None:
            return
        request = getattr(self._result, "request", None)
        if request is not None:
            self._start(replace(request, verification="full"))
        else:
            self._run_cli(["model", "test", "--out", str(self._out_dir), "--json"])

    def _install(self) -> None:
        if self._out_dir is None:
            return
        try:
            from boardmodeler.authoring.card import plan_install
            from boardmodeler.authoring.spec import SpecSet

            spec = SpecSet.from_json(
                (self._out_dir / "spec" / "characteristics.json").read_text(encoding="utf-8")
            )
            plan = plan_install(
                part=spec.part,
                subckt=spec.subckt,
                lib=self._out_dir / f"{spec.subckt}.lib",
                asy=self._out_dir / f"{spec.subckt}.asy",
                user_lib=True,
                apply=True,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Could not install", str(exc))
            return
        QMessageBox.information(
            self,
            "Installed",
            (
                "Files saved inside this app folder. Add library/sym and library/sub to "
                "LTspice's search paths once:\n\n"
                if portable()
                else "LTspice will find the model the next time it starts:\n\n"
            )
            + "\n".join(plan.steps[:2])
            + f"\n\nPlace the {spec.subckt} symbol on a schematic, or open example.cir "
            "from the model folder.",
        )

    def _open_folder(self) -> None:
        if self._out_dir is None:
            return
        path = str(self._out_dir)
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)

    def _run_cli(self, argv: list[str]) -> None:
        """Reuse the CLI for the follow-up actions so both surfaces behave identically."""
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "boardmodeler.cli", *argv],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Command failed", str(exc))
            return
        message = completed.stdout.strip() or completed.stderr.strip() or "no output"
        if list(argv[:2]) == ["doctor", "--json"]:
            # The report goes to its own page, whole: this used to be a message box showing
            # the *last* 4000 characters, which hid the head of the report and could not be
            # scrolled usefully. The invocation stays exactly what ``doctor --json`` is.
            exit_code = getattr(completed, "returncode", 0)
            self._show_doctor(message, exit_code=exit_code if isinstance(exit_code, int) else 0)
            return
        QMessageBox.information(self, "boardmodeler " + " ".join(argv[:2]), message[-4000:])

    def _show_doctor(self, report: str, *, exit_code: int = 0) -> DoctorView:
        """Show the whole report on its own resizable page and keep the handle for tests."""
        view = DoctorView(self)
        view.set_report(report, exit_code=exit_code)
        self.doctor_view = view
        view.show()
        view.raise_()
        return view

    def _open_setup(self) -> None:
        from boardmodeler.ui.setup_dialog import SetupDialog

        dialog = SetupDialog(self)
        dialog.exec()
        self.again_button.setEnabled(self._result is not None)

    def _run_doctor(self) -> None:
        self._run_cli(["doctor", "--json"])

    # ------------------------------------------------------------------ signals
    def _stage_item(self, row: int, column: int) -> QTableWidgetItem:
        """One cell of the stage table: every row is inserted complete, so items exist."""
        item = self.stages.item(row, column)
        assert item is not None
        return item

    def _on_stage(self, event: object) -> None:
        stage = getattr(event, "stage", "?")
        status = getattr(event, "status", "?")
        detail = getattr(event, "detail", "")
        counts = getattr(event, "counts", {}) or {}
        if counts:
            detail = f"{detail} {counts}".strip()
        for row in range(self.stages.rowCount()):
            if self._stage_item(row, 0).text() == stage:
                self._stage_item(row, 1).setText(status)
                self._stage_item(row, 1).setForeground(
                    _colour(_STATUS_COLOUR.get(status, "#ffffff"))
                )
                self._stage_item(row, 2).setText(detail)
                self._stage_item(row, 2).setToolTip(detail)
                break
        else:
            row = self.stages.rowCount()
            self.stages.insertRow(row)
            self.stages.setItem(row, 0, QTableWidgetItem(stage))
            item = QTableWidgetItem(status)
            item.setForeground(_colour(_STATUS_COLOUR.get(status, "#ffffff")))
            self.stages.setItem(row, 1, item)
            self.stages.setItem(row, 2, QTableWidgetItem(detail))
            self._stage_item(row, 2).setToolTip(detail)
        self.status_label.setText(f"{stage}: {detail}".strip()[:160])
        self.status_label.setToolTip(detail)

    def _on_result(self, result: object) -> None:
        self._result = result
        self._set_busy(False)
        status = getattr(result, "status", "UNKNOWN")
        counts = getattr(result, "counts", {}) or {}
        detail = getattr(result, "detail", "")
        self.status_label.setText(f"{status} — {counts} — {detail}"[:200])
        self.status_label.setToolTip(detail)
        self.status_label.setStyleSheet(
            f"color: {_STATUS_COLOUR.get(status, '#ffffff')}; font-family: Consolas; "
            "font-size: 10pt;"
        )
        quick = getattr(getattr(result, "request", None), "verification", "full") == "sanity"
        if quick and getattr(result, "lib_path", None) is not None:
            self.status_label.setText("SANITY CHECKED — electrical accuracy unverified")
            self.status_label.setStyleSheet("color: #55ffff;")
        self.again_button.setText("Run full verification" if quick else "Run tests again")
        rows = getattr(result, "rows", ()) or ()
        self.rows.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                getattr(row, "req_id", ""),
                getattr(row, "required", ""),
                getattr(row, "measured", ""),
                getattr(row, "status", ""),
                str(getattr(row, "page", "") or ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 3:
                    item.setForeground(_colour(_STATUS_COLOUR.get(str(value), "#ffffff")))
                if column == 0:
                    item.setToolTip(getattr(row, "statement", ""))
                self.rows.setItem(index, column, item)
        self.open_button.setEnabled(True)
        self.install_button.setEnabled(getattr(result, "lib_path", None) is not None)
        self.again_button.setEnabled(True)
        self._worker = None

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.setText("failed — " + message[:160])
        self.status_label.setStyleSheet("color: #ff5555; font-family: Consolas; font-size: 10pt;")
        QMessageBox.critical(self, "The run failed", message)
        self._worker = None

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("cancelling…")

    def closeEvent(self, event: object) -> None:  # Qt signature
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(event)  # type: ignore[arg-type]


def _smallest_useful(content_minimum: QSize) -> QSize:
    """The size the window may be shrunk to: what the content needs, plus a small margin.

    A floor taken from the layout keeps every control reachable; the margin keeps the
    outermost border and the table frames from touching the window edge at that size.
    """
    return QSize(content_minimum.width() + 8, content_minimum.height() + 8)


def _default_model_dir() -> str:
    """Where models go by default: the persisted setting, else a folder in the home dir."""
    try:
        from boardmodeler.config import load_config

        configured = load_config().default_model_dir
    except Exception:  # pragma: no cover - a broken config must not block the window
        configured = None
    return str(model_dir(configured))


def _window_stylesheet() -> str:
    """The retro control sheet plus window-scoped rules only.

    The earlier version appended a bare ``QWidget`` rule, which tied with
    ``QPushButton`` on specificity and, being later, won — every button was painted
    black while the button text stayed black, so an enabled button was invisible.
    Here the background is scoped by object name and the button styling comes from
    ``RETRO_STYLESHEET``, so a window rule can never repaint a control.
    """
    return (
        RETRO_STYLESHEET
        + f"""
QMainWindow, #root {{ background: {CGA["black"]}; }}
QMainWindow QLabel {{ color: {CGA["grey"]}; font-family: Consolas; font-size: 10pt; }}
QTableWidget {{ background: {CGA["black"]}; color: {CGA["bright_green"]};
                gridline-color: {CGA["dark_grey"]}; font-family: Consolas; font-size: 10pt;
                border: 2px solid {CGA["bright_blue"]}; }}
QHeaderView::section {{ background: {CGA["blue"]}; color: {CGA["white"]};
                        border: 0; padding: 3px; font-family: Consolas; }}
QTableCornerButton::section {{ background: {CGA["blue"]}; }}
"""
    )
