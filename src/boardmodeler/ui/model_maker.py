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

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from boardmodeler.storage import local_path, model_dir, portable

__all__ = ["ModelMakerWindow"]

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


def _configured_provider() -> object:
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


def _colour(value: str) -> object:
    from PySide6.QtGui import QColor

    return QColor(value)


class ModelMakerWindow(QMainWindow):
    """Part number + datasheet + save location -> agent-authored, simulator-judged model."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_window_title())
        self.setFixedSize(900, 600)
        self._worker: MakeModelWorker | None = None
        self._result: object | None = None
        self._out_dir: Path | None = None
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
        self.elapsed_label = QLabel("ELAPSED 00:00:00")
        self.elapsed_label.setStyleSheet("color: #55ffff; font-family: Consolas;")
        self.elapsed_label.setToolTip(
            "Time since this build started (hours:minutes:seconds). "
            "Includes waiting for the agent and cancellation; not an estimate of time remaining."
        )
        row.addWidget(self.elapsed_label)
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
            self.datasheet_edit.text() or str(Path.home()),
            "PDF (*.pdf)",
        )
        if path:
            self.datasheet_edit.setText(path)
            if not self.part_edit.text().strip():
                self.part_edit.setText(Path(path).stem.split("_")[0].upper())

    def _choose_out(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Where should the model be saved?", self.out_edit.text() or str(Path.home())
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
        self._update_elapsed()

    def _elapsed(self) -> str:
        seconds = self._elapsed_seconds
        if self._started_at is not None:
            seconds = time.monotonic() - self._started_at
        hours, remainder = divmod(max(0, int(seconds)), 3600)
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
        QMessageBox.information(self, "boardmodeler " + " ".join(argv[:2]), message[-4000:])

    def _open_setup(self) -> None:
        from boardmodeler.ui.setup_dialog import SetupDialog

        dialog = SetupDialog(self)
        dialog.exec()
        self.again_button.setEnabled(self._result is not None)

    def _run_doctor(self) -> None:
        self._run_cli(["doctor", "--json"])

    # ------------------------------------------------------------------ signals
    def _on_stage(self, event: object) -> None:
        stage = getattr(event, "stage", "?")
        status = getattr(event, "status", "?")
        detail = getattr(event, "detail", "")
        counts = getattr(event, "counts", {}) or {}
        if counts:
            detail = f"{detail} {counts}".strip()
        for row in range(self.stages.rowCount()):
            if self.stages.item(row, 0).text() == stage:
                self.stages.item(row, 1).setText(status)
                self.stages.item(row, 1).setForeground(
                    _colour(_STATUS_COLOUR.get(status, "#ffffff"))
                )
                self.stages.item(row, 2).setText(detail)
                self.stages.item(row, 2).setToolTip(detail)
                break
        else:
            row = self.stages.rowCount()
            self.stages.insertRow(row)
            self.stages.setItem(row, 0, QTableWidgetItem(stage))
            item = QTableWidgetItem(status)
            item.setForeground(_colour(_STATUS_COLOUR.get(status, "#ffffff")))
            self.stages.setItem(row, 1, item)
            self.stages.setItem(row, 2, QTableWidgetItem(detail))
            self.stages.item(row, 2).setToolTip(detail)
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
    from boardmodeler.ui.theme import CGA, RETRO_STYLESHEET

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
