"""The model maker: part number + datasheet + save location in, judged model out.

This window is the product and it holds only what a build needs: which part, which
datasheet, where the model goes, a GO button, and the progress of the run. Settings that
persist between sessions — the LTspice path, the agent's API key, the default model
folder, the LTspice library location, the INTERNET ACCESS switch — live in
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

from PySide6.QtCore import QPointF, QRectF, QSize, QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from boardmodeler.agent_providers import AgentProvider
from boardmodeler.storage import local_path, model_dir, portable
from boardmodeler.ui.file_dialogs import normalize_path_text, starting_directory
from boardmodeler.ui.pdf_input import PdfPathEdit, dropped_pdf, readable_pdf
from boardmodeler.ui.retest_worker import RetestWorker
from boardmodeler.ui.theme import DESKTOP, RETRO_STYLESHEET

__all__ = ["DoctorView", "HourglassWidget", "ModelMakerWindow", "readable_doctor_report"]

_STATUS_COLOUR = {
    "PASS": DESKTOP["pass"],
    "FAIL": DESKTOP["fail"],
    "UNKNOWN": DESKTOP["unknown"],
    "BLOCKED": DESKTOP["fail"],
    "NOT_APPLICABLE": DESKTOP["muted"],
    "running": DESKTOP["blue"],
    "ok": DESKTOP["pass"],
    "failed": DESKTOP["fail"],
    "skipped": DESKTOP["muted"],
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


def _configured_full_verification() -> bool:
    """The remembered default for the per-build checkbox beside GO."""
    try:
        from boardmodeler.config import load_config

        return bool(load_config().full_verification)
    except Exception:  # pragma: no cover - a broken config must not block the window
        return True


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

        glass = QPen(QColor(DESKTOP["navy"]))
        glass.setWidthF(1.0)
        painter.setPen(glass)
        painter.drawLine(QPointF(2.5, 2.0), QPointF(15.5, 2.0))
        painter.drawLine(QPointF(2.5, 20.0), QPointF(15.5, 20.0))
        painter.drawPolyline(QPolygonF([QPointF(4.0, 3.0), QPointF(9.0, 11.0), QPointF(4.0, 19.0)]))
        painter.drawPolyline(
            QPolygonF([QPointF(14.0, 3.0), QPointF(9.0, 11.0), QPointF(14.0, 19.0)])
        )

        sand = QColor(DESKTOP["navy"])
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
            f"color: {DESKTOP['blue']}; font-family: 'Segoe UI'; font-size: 9pt;"
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
    """Per-build inputs and engine choice -> simulator-judged model."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_window_title())
        # Start at the content size; expanded diagnostics remain resizable.
        self._worker: MakeModelWorker | RetestWorker | None = None
        self._result: object | None = None
        self._out_dir: Path | None = None
        self.doctor_view: DoctorView | None = None
        self._started_at: float | None = None
        self._elapsed_seconds = 0.0
        self._model_root = Path(_default_model_dir())
        self._automatic_output = True
        self._setting_output = False
        self._datasheet_folder = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        self.setAcceptDrops(True)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        self.setStyleSheet(_window_stylesheet())
        central = QWidget(self)
        central.setObjectName("root")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("modelScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll_area)
        self.page = QWidget()
        self.page.setObjectName("modelPage")
        self.scroll_area.setWidget(self.page)
        layout = QVBoxLayout(self.page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        self._content_layout = layout

        layout.addLayout(self._build_top_row())
        layout.addLayout(self._build_inputs())
        layout.addLayout(self._build_actions())
        layout.addWidget(self._build_result_header())
        layout.addWidget(self.progress)
        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        self.counts_label.hide()
        layout.addWidget(self.counts_label)
        layout.addLayout(self._build_result_actions())
        self.saved_location = QLabel()
        self.saved_location.setWordWrap(True)
        self.saved_location.hide()
        layout.addWidget(self.saved_location)
        self.advanced_button = QPushButton("Advanced")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_panel)
        self.details_button = QPushButton("Show details")
        self.details_button.setCheckable(True)
        self.details_button.setToolTip(
            "Tool readiness, build stages and the unchanged test results"
        )
        self.details_button.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_button)
        self.details_panel = QWidget()
        details = QVBoxLayout(self.details_panel)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(8)
        details.addWidget(self.readiness)
        details.addWidget(self.environment_button)
        details.addWidget(self._build_stages())
        details.addWidget(self._build_rows())
        self.details_panel.hide()
        layout.addWidget(self.details_panel, 1)
        # Spare height belongs below the compact form. Diagnostics use it when open.
        layout.addStretch(1)
        self._bottom_space_index = layout.count() - 1
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        self.part_edit.textChanged.connect(self._part_changed)
        self.out_edit.textChanged.connect(self._output_changed)
        self._engine_changed()
        self._part_changed()
        layout.activate()
        self.resize(self.minimumSize())

    # ------------------------------------------------------------------ widgets
    def _build_top_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        banner = QLabel("Spice Maker")
        banner.setObjectName("windowBanner")
        banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(banner, 1)
        setup = QPushButton("SETUP")
        setup.setToolTip("LTspice path, agent key, model folder, INTERNET ACCESS")
        setup.clicked.connect(self._open_setup)
        check = QPushButton("CHECK ENVIRONMENT")
        check.setToolTip("Where LTspice, the reader backend and the agent key stand")
        check.clicked.connect(self._run_doctor)
        row.addWidget(setup)
        self.environment_button = check
        # Readiness lights + VERIFY KEY & TOOLS: the key, the model's reply format,
        # LTspice, the PDF reader, OCR and the network switch, visible before GO.
        from boardmodeler.ui.readiness_strip import ReadinessStrip

        # Its own row (added in __init__), so the lights never widen the window's floor.
        self.readiness = ReadinessStrip()
        QTimer.singleShot(0, self.readiness, self.readiness.refresh_local)
        self.setup_hint = QLabel("")
        self.setup_hint.setStyleSheet(f"color: {DESKTOP['fail']};")
        row.addWidget(self.setup_hint)
        return row

    def _build_inputs(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)

        self.part_edit = QLineEdit()
        self.part_edit.setPlaceholderText("Full part number, e.g. UCC28251PW")
        self.part_edit.setAcceptDrops(False)
        grid.addWidget(QLabel("PART NUMBER"), 0, 0)
        grid.addWidget(self.part_edit, 0, 1, 1, 2)

        self.datasheet_edit = PdfPathEdit()
        self.datasheet_edit.setMinimumWidth(220)
        self.datasheet_edit.setPlaceholderText("Drop a PDF here, or choose a file")
        self.datasheet_edit.pdf_dropped.connect(self._select_datasheet)
        browse_pdf = QPushButton("Choose PDF…")
        self.datasheet_button = browse_pdf
        browse_pdf.clicked.connect(self._choose_datasheet)
        grid.addWidget(QLabel("DATASHEET"), 1, 0)
        grid.addWidget(self.datasheet_edit, 1, 1)
        grid.addWidget(browse_pdf, 1, 2)

        self.advanced_panel = QWidget()
        advanced = QGridLayout(self.advanced_panel)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setHorizontalSpacing(8)
        advanced.setVerticalSpacing(7)
        self.advanced_panel.hide()
        self._advanced_layout = advanced

        self.out_edit = QLineEdit(str(self._model_root))
        self.out_edit.setAcceptDrops(False)
        self.out_edit.setToolTip(
            "Each part gets a folder under your configured model folder. "
            "Type or choose a different folder to use that exact location."
        )
        browse_out = QPushButton("Choose folder…")
        self.output_button = browse_out
        browse_out.clicked.connect(self._choose_out)
        advanced.addWidget(QLabel("SAVE MODEL TO"), 0, 0)
        advanced.addWidget(self.out_edit, 0, 1)
        advanced.addWidget(browse_out, 0, 2)

        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Automatic (recommended)", "behavioral")
        self.engine_combo.addItem("AI authored (legacy)", "legacy_ai")
        self.engine_combo.addItem("Pins only (no functional behavior)", "pin_only")
        advanced.addWidget(QLabel("BUILD MODE"), 1, 0)
        advanced.addWidget(self.engine_combo, 1, 1, 1, 2)

        from boardmodeler.models.support import ORDINARY_FAMILIES

        self.family_combo = QComboBox()
        self.family_combo.addItem("Automatic from the datasheet", None)
        for family_id, label, _phrases in ORDINARY_FAMILIES:
            self.family_combo.addItem(label, family_id)
        self.family_combo.setToolTip(
            "Optional classification hint for this build. Choosing a family does not add model support."
        )
        advanced.addWidget(QLabel("FAMILY HINT"), 2, 0)
        advanced.addWidget(self.family_combo, 2, 1, 1, 2)

        self.engine_hint = QLabel()
        self.engine_hint.setWordWrap(True)
        self.engine_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        advanced.addWidget(self.engine_hint, 4, 0, 1, 3)
        return grid

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.go_button = QPushButton("Make Model")
        self.go_button.setToolTip("Find or build a model for this part")
        self.go_button.setDefault(True)
        self.go_button.clicked.connect(self._make_model)
        self.cancel_button = QPushButton("CANCEL")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.go_button, 3)
        row.addWidget(self.cancel_button, 1)
        # This legacy-only per-build choice stays in Advanced. Its remembered setting
        # never changes the full-verification policy of the automatic route.
        self.full_check = QCheckBox("FULL VERIFICATION")
        self.full_check.setToolTip(
            "On: run LTspice checks against the datasheet facts. "
            "Off: the explicit legacy route only checks structure; electrical accuracy "
            "remains unverified. Remembered for the next legacy build."
        )
        self.full_check.setChecked(_configured_full_verification())
        self._legacy_full_verification = self.full_check.isChecked()
        self.full_check.toggled.connect(self._full_verification_toggled)
        self._advanced_layout.addWidget(self.full_check, 3, 0, 1, 3)
        # The hourglass sits immediately beside the clock it belongs to, and both are driven
        # by the same two places: _set_busy starts them on GO and stops them on every exit.
        self.hourglass = HourglassWidget()
        self.hourglass.setObjectName("hourglass")
        row.addWidget(self.hourglass)
        self.elapsed_label = QLabel("ELAPSED 00:00:00")
        self.elapsed_label.setStyleSheet(
            f"color: {DESKTOP['navy']}; font-family: Consolas; font-weight: 600;"
        )
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
        self.status_label = QLabel("Choose a datasheet and enter the part number.")
        # Wrapped, not clipped: a status line can carry a whole failure reason, and an
        # unwrapped QLabel would force the window's minimum width to the full sentence.
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {DESKTOP['text']};")
        row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.progress.setToolTip(
            "The moving blocks show work is active. They do not estimate percentage or time remaining."
        )
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
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        self.install_button = QPushButton("Add to LTspice")
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
    def _part_changed(self) -> None:
        if self._automatic_output:
            part = normalize_path_text(self.part_edit.text()).upper()
            name = "".join(
                character if character.isalnum() or character in "_-" else "_" for character in part
            )
            self._setting_output = True
            try:
                self.out_edit.setText(
                    str(self._model_root / name) if name else str(self._model_root)
                )
            finally:
                self._setting_output = False
        self._sync_content_size()

    def _output_changed(self) -> None:
        if not self._setting_output:
            self._automatic_output = False

    def _toggle_details(self, visible: bool) -> None:
        self.details_panel.setVisible(visible)
        self.details_button.setText("Hide details" if visible else "Show details")
        self._content_layout.setStretch(self._bottom_space_index, 0 if visible else 1)
        self._sync_content_size(grow=visible)

    def _toggle_advanced(self, visible: bool) -> None:
        self.advanced_panel.setVisible(visible)
        self.advanced_button.setText("Hide advanced" if visible else "Advanced")
        self._sync_content_size(grow=visible)

    def _sync_content_size(self, *, grow: bool = False) -> None:
        """Keep the compact restore floor; scroll expanded content on a small screen."""
        self._content_layout.activate()
        if not hasattr(self, "_compact_minimum"):
            self._compact_minimum = _smallest_useful(self._content_layout.minimumSize())
        self.setMinimumSize(self._compact_minimum)
        if grow and not self.isMaximized() and not self.isFullScreen():
            preferred = self.page.sizeHint()
            available = self.screen().availableGeometry().size()
            frame = self.frameGeometry().size() - self.size()
            available -= QSize(max(0, frame.width()), max(0, frame.height()))
            self.resize(
                max(self.width(), min(available.width(), preferred.width())),
                max(self.height(), min(available.height(), preferred.height())),
            )

    def _engine_changed(self) -> None:
        engine = self.engine_combo.currentData()
        legacy = engine == "legacy_ai"
        # Requiring full verification for a code-built route must not overwrite the
        # user's remembered choice for legacy builds.
        previous = self.full_check.blockSignals(True)
        self.full_check.setChecked(self._legacy_full_verification if legacy else True)
        self.full_check.blockSignals(previous)
        self.full_check.setEnabled(legacy and self.go_button.isEnabled())
        self.full_check.setVisible(legacy)
        hints = {
            "legacy_ai": (
                "The provider selected in SETUP writes and repairs model text. "
                "This legacy route sends datasheet and model text to that provider."
            ),
            "behavioral": (
                "Use a compatible manufacturer model first; otherwise use reviewed generation. "
                "Generated functions are limited to TPS54332DDA, LM358 and UCC28251PW. "
                "Extraction may still need the configured provider."
            ),
            "pin_only": (
                "Creates a pin interface without functional behavior or an electrical accuracy "
                "claim. Full verification is required. Extraction may still need the provider."
            ),
        }
        self.engine_hint.setText(hints[engine])

    def _choose_datasheet(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose the datasheet",
            # A folder, always: the field holds a PDF path, and Qt's third argument is
            # where the dialog opens, so a PDF (or a stale one) would fall back to C:\\.
            starting_directory(self.datasheet_edit.text(), fallback=self._datasheet_folder),
            "PDF (*.pdf);;All files (*)",
        )
        if path:
            self._select_datasheet(path)

    def _select_datasheet(self, value: str) -> None:
        path = Path(normalize_path_text(value))
        self.datasheet_edit.setText(str(path))
        self.datasheet_edit.setToolTip(str(path))
        self._datasheet_folder = str(path.parent)

    def dragEnterEvent(self, event) -> None:
        if (
            self.go_button.isEnabled()
            and event.possibleActions() & Qt.DropAction.CopyAction
            and dropped_pdf(event.mimeData()) is not None
        ):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        path = dropped_pdf(event.mimeData())
        if (
            self.go_button.isEnabled()
            and event.possibleActions() & Qt.DropAction.CopyAction
            and path is not None
        ):
            self._select_datasheet(str(path))
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

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
        for widget in (
            self.part_edit,
            self.datasheet_edit,
            self.out_edit,
            self.datasheet_button,
            self.output_button,
        ):
            widget.setEnabled(not busy)
        self.go_button.setEnabled(not busy)
        self.engine_combo.setEnabled(not busy)
        self.family_combo.setEnabled(not busy)
        self.full_check.setEnabled(not busy and self.engine_combo.currentData() == "legacy_ai")
        self.cancel_button.setEnabled(busy)
        self.again_button.setEnabled(
            not busy and self._result is not None and not self._official_original(self._result)
        )
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setTextVisible(not busy)
        if not busy:
            self.progress.setValue(0)
            self.progress.setFormat("Stopped")
        if busy:
            self._started_at = time.monotonic()
            self._elapsed_seconds = 0.0
            self._elapsed_timer.start()
            self.hourglass.start()
            self.stages.setRowCount(0)
            self.rows.setRowCount(0)
            self.counts_label.clear()
            self.counts_label.hide()
            self._result = None
            self.saved_location.hide()
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
        part = normalize_path_text(self.part_edit.text())
        if part.upper() == "UCC28251":
            ordering_code, accepted = QInputDialog.getText(
                self,
                "Full part number needed",
                "UCC28251 uses different pin numbers in PW and RGP packages.\n"
                "Enter the full ordering code, for example UCC28251PW or UCC28251RGP.",
            )
            if not accepted:
                return
            part = normalize_path_text(ordering_code).upper()
            if part not in ("UCC28251PW", "UCC28251PWR", "UCC28251RGP", "UCC28251RGPR"):
                QMessageBox.warning(
                    self,
                    "Full part number needed",
                    "Enter a PW or RGP ordering code; the package cannot be guessed from this datasheet.",
                )
                return
            self.part_edit.setText(part)
        datasheet = Path(normalize_path_text(self.datasheet_edit.text()))
        out_dir = Path(normalize_path_text(self.out_edit.text()) or _default_model_dir())
        try:
            out_dir = local_path(out_dir)
        except ValueError as exc:
            QMessageBox.warning(self, "Choose a local folder", str(exc))
            return
        if not part:
            QMessageBox.warning(self, "Part number needed", "Which part should be modelled?")
            return
        if not readable_pdf(datasheet):
            QMessageBox.warning(self, "Datasheet needed", f"Choose a readable PDF: {datasheet}")
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

        engine = self.engine_combo.currentData()
        provider = _configured_provider()
        # Code-built routes may have reviewed local extraction and need no provider.
        # The pipeline checks availability only when extraction actually needs one.
        usable, reason = _agent_availability() if engine == "legacy_ai" else (True, "")
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
        try:
            provider_id = provider.id if provider is not None else _configured_provider_id()
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Settings not readable",
                f"Nothing was started because the saved settings could not be read:\n\n{exc}",
            )
            return
        out_dir.mkdir(parents=True, exist_ok=True)

        subckt = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in part).upper()

        request = MakeModelRequest(
            part=part,
            subckt=subckt,
            datasheet=datasheet,
            out_dir=out_dir,
            backend_name="api",
            verification="full"
            if engine != "legacy_ai" or self.full_check.isChecked()
            else "sanity",
            engine=engine,
            family=self.family_combo.currentData(),
            plan_tests=False,
            allow_remote=True,
            # The configured id as written, so ``build_api_backend`` refuses a provider
            # this build lacks instead of another provider answering with the wrong key.
            provider=provider_id,
        )
        self._out_dir = out_dir
        self._start(request)

    def _full_verification_toggled(self, checked: bool) -> None:
        """Remember the choice without letting a settings error escape a Qt slot."""
        self._legacy_full_verification = bool(checked)
        try:
            from boardmodeler.config import load_config, save_config

            config = load_config()
            config.full_verification = bool(checked)
            save_config(config)
        except Exception as exc:
            self.status_label.setToolTip(
                f"FULL VERIFICATION could not be saved as the default: {type(exc).__name__}: {exc}"
            )

    def _start(self, request: object) -> None:
        self._set_busy(True)
        self.status_label.setText("working…")
        self.status_label.setStyleSheet(f"color: {DESKTOP['blue']};")
        worker = MakeModelWorker(request, self)
        worker.progressed.connect(self._on_stage)
        worker.finished_result.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _rerun_tests(self) -> None:
        if self._out_dir is None:
            return
        if self._official_original(self._result):
            return
        request = getattr(self._result, "request", None)
        if request is not None and getattr(request, "verification", "full") == "sanity":
            self._start(replace(request, verification="full"))
        else:
            self._set_busy(True)
            self.status_label.setText("Testing saved model…")
            worker = RetestWorker(self._out_dir, request, self)
            worker.progressed.connect(self._on_stage)
            worker.finished_result.connect(self._on_result)
            worker.failed.connect(self._on_failed)
            self._worker = worker
            worker.start()

    def _install(self) -> None:
        if self._out_dir is None:
            return
        if self._official_original(self._result):
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
        if self._automatic_output:
            self._model_root = Path(_default_model_dir())
            self._part_changed()
        self.again_button.setEnabled(
            self._result is not None and not self._official_original(self._result)
        )
        self.readiness.refresh_local()

    def _run_doctor(self) -> None:
        self._run_cli(["doctor", "--json"])

    # ------------------------------------------------------------------ signals
    def _stage_item(self, row: int, column: int) -> QTableWidgetItem:
        """One cell of the stage table: every row is inserted complete, so items exist."""
        item = self.stages.item(row, column)
        assert item is not None
        return item

    def _finish_stages(self, status: str, detail: str) -> None:
        """Reconcile the stage table with a terminal result or error."""
        fallback = (
            status == "UNKNOWN" and "template retained after bounded repair" in detail.lower()
        )
        for row in range(self.stages.rowCount()):
            stage = self._stage_item(row, 0).text()
            state = self._stage_item(row, 1)
            previous_detail = self._stage_item(row, 2).text()
            if state.text() != "running" and not (fallback and stage == "author"):
                continue
            state.setText(status)
            state.setForeground(_colour(_STATUS_COLOUR.get(status, DESKTOP["text"])))
            final_detail = previous_detail if fallback and stage == "author" else detail
            final_detail = final_detail or f"Build ended: {status}"
            self._stage_item(row, 2).setText(final_detail)
            self._stage_item(row, 2).setToolTip(final_detail)

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
                    _colour(_STATUS_COLOUR.get(status, DESKTOP["text"]))
                )
                self._stage_item(row, 2).setText(detail)
                self._stage_item(row, 2).setToolTip(detail)
                break
        else:
            row = self.stages.rowCount()
            self.stages.insertRow(row)
            self.stages.setItem(row, 0, QTableWidgetItem(stage))
            item = QTableWidgetItem(status)
            item.setForeground(_colour(_STATUS_COLOUR.get(status, DESKTOP["text"])))
            self.stages.setItem(row, 1, item)
            self.stages.setItem(row, 2, QTableWidgetItem(detail))
            self._stage_item(row, 2).setToolTip(detail)
        self.status_label.setText(f"{stage}: {detail}".strip()[:160])
        self.status_label.setToolTip(detail)

    def _official_original(self, result: object) -> bool:
        library = getattr(result, "lib_path", None)
        folder = getattr(result, "out_dir", None) or self._out_dir
        if library is None or folder is None:
            return False
        try:
            return Path(library).relative_to(Path(folder)).parts[0] == "vendor-originals"
        except ValueError, IndexError:
            return False

    def _on_result(self, result: object) -> None:
        self._result = result
        self._set_busy(False)
        status = getattr(result, "status", "UNKNOWN")
        counts = getattr(result, "counts", {}) or {}
        detail = getattr(result, "detail", "")
        self._finish_stages(str(status), str(detail))
        headline = (
            "Model saved — UNKNOWN (limited coverage)"
            if status == "UNKNOWN" and getattr(result, "lib_path", None) is not None
            else str(status)
        )
        self.status_label.setText(f"{headline} — {detail}"[:200])
        self.status_label.setToolTip(detail)
        self.counts_label.setText(
            " · ".join(
                f"{count} {str(state).lower().replace('_', ' ')}"
                for state, count in counts.items()
                if count
            )
        )
        self.counts_label.setVisible(bool(counts))
        self.progress.setFormat("Finished")
        self.saved_location.setText(f"Saved to: {self._out_dir or getattr(result, 'out_dir', '')}")
        self.saved_location.setVisible(getattr(result, "lib_path", None) is not None)
        self.status_label.setStyleSheet(f"color: {_STATUS_COLOUR.get(status, DESKTOP['text'])};")
        quick = getattr(getattr(result, "request", None), "verification", "full") == "sanity"
        if quick and getattr(result, "lib_path", None) is not None:
            self.status_label.setText("SANITY CHECKED — electrical accuracy unverified")
            self.status_label.setStyleSheet(f"color: {DESKTOP['blue']};")
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
                    item.setForeground(_colour(_STATUS_COLOUR.get(str(value), DESKTOP["text"])))
                if column == 0:
                    item.setToolTip(getattr(row, "statement", ""))
                self.rows.setItem(index, column, item)
        self.open_button.setEnabled(True)
        official = self._official_original(result)
        self.install_button.setEnabled(
            getattr(result, "lib_path", None) is not None and not official
        )
        self.again_button.setEnabled(not official)
        self.install_button.setToolTip(
            "Manufacturer originals are saved with their supporting files. Open the model folder to use them."
            if official
            else ""
        )
        self.again_button.setToolTip(
            "This route checks compatibility only; datasheet verification is unavailable for the original model."
            if official
            else ""
        )
        self._worker = None

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._finish_stages("failed", "Build stopped with an error")
        self.status_label.setText("failed — " + message[:160])
        self.status_label.setStyleSheet(f"color: {DESKTOP['fail']};")
        QMessageBox.critical(self, "The run failed", message)
        self._worker = None

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("cancelling…")

    def closeEvent(self, event: object) -> None:  # Qt signature
        self.readiness.cancel()
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
QMainWindow, #root, #modelPage, QScrollArea#modelScroll {{ background: {DESKTOP["face"]}; border: 0; }}
QTableWidget {{ background: white; color: {DESKTOP["text"]};
    gridline-color: #dedede; font-family: "Segoe UI"; font-size: 10pt;
    border: 2px inset {DESKTOP["shadow"]}; }}
QHeaderView::section {{ background: {DESKTOP["face"]}; color: {DESKTOP["text"]};
    border: 1px solid {DESKTOP["shadow"]}; padding: 5px; font-family: "Segoe UI"; }}
QTableCornerButton::section {{ background: {DESKTOP["face"]}; }}
"""
    )
