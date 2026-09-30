"""Main window (D12): inputs, stages, results, review, waveforms.

One ``QMainWindow``; jobs run in the worker child process and this window only
renders the events it emits (the GUI never computes a verdict). The accessors
``results_table()``, ``stage_list()``, ``findings_list()``, ``review_panel()``
and ``waveform_view()`` exist so tests can assert what the window displays
without simulating mouse input.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from boardmodeler.config import AppConfig, config_path, load_config
from boardmodeler.pipeline.project import Project, ProjectError
from boardmodeler.ui.results_panel import STATUS_COLOURS, ResultsPanel
from boardmodeler.ui.review_panel import ReviewPanel
from boardmodeler.ui.waveforms import WaveformView
from boardmodeler.ui.worker_client import WorkerClient

__all__ = ["DEFAULT_USE_PROFILE", "InputsPanel", "MainWindow"]

DEFAULT_USE_PROFILE = "Power and I/O sequencing"
MAX_WAVEFORM_BYTES = 64 * 1024 * 1024
"""Above this the window refuses to load a ``.raw`` (it would freeze the GUI)."""
SCOPES = (
    "(all)",
    "circuit_compliance",
    "fault_detection",
    "model_qualification",
    "primitive_reference",
)
PROVIDERS = ("(default)", "fixture", "http_inference", "bob_direct", "bob_shell")


class InputsPanel(QGroupBox):
    """Documents, identity/mode, circuit file, profile and the run button."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Inputs", parent)
        self.project_edit = QLineEdit(self)
        self.project_edit.setPlaceholderText("project directory")
        browse_project = QPushButton("...", self)
        browse_project.setFixedWidth(30)
        browse_project.clicked.connect(self._browse_project)
        project_row = QHBoxLayout()
        project_row.addWidget(self.project_edit)
        project_row.addWidget(browse_project)
        project_widget = QWidget(self)
        project_widget.setLayout(project_row)

        folder_button = QPushButton("Open project...", self)
        folder_button.clicked.connect(self._browse_project)

        self.document_list = QListWidget(self)
        self.document_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        add_document = QPushButton("Add files...", self)
        add_document.clicked.connect(self._add_documents)
        remove_document = QPushButton("Remove", self)
        remove_document.clicked.connect(self._remove_documents)
        documents_row = QHBoxLayout()
        documents_row.addWidget(add_document)
        documents_row.addWidget(remove_document)

        self.mode = QComboBox(self)
        self.mode.addItems(["component", "circuit"])
        self.mode.currentTextChanged.connect(self._mode_changed)
        self.circuit_edit = QLineEdit(self)
        self.circuit_edit.setPlaceholderText("circuit file (.asc / .cir / .net)")
        browse_circuit = QPushButton("...", self)
        browse_circuit.setFixedWidth(30)
        browse_circuit.clicked.connect(self._browse_circuit)
        circuit_row = QHBoxLayout()
        circuit_row.addWidget(self.circuit_edit)
        circuit_row.addWidget(browse_circuit)
        circuit_widget = QWidget(self)
        circuit_widget.setLayout(circuit_row)
        self.circuit_widget = circuit_widget

        self.use_profile = QComboBox(self)
        self.use_profile.setEditable(True)
        self.use_profile.addItems([DEFAULT_USE_PROFILE])

        self.part_manufacturer = QLineEdit(self)
        self.part_manufacturer.setPlaceholderText("manufacturer (optional)")
        self.part_ordering_code = QLineEdit(self)
        self.part_ordering_code.setPlaceholderText("ordering code (optional)")

        self.scope = QComboBox(self)
        self.scope.addItems(list(SCOPES))

        self.provider = QComboBox(self)
        self.provider.addItems(list(PROVIDERS))

        self.allow_remote = QPushButton("Allow remote inference: OFF", self)
        self.allow_remote.setCheckable(True)
        self.allow_remote.toggled.connect(
            lambda checked: self.allow_remote.setText(
                f"Allow remote inference: {'ON' if checked else 'OFF'}"
            )
        )

        self.run_button = QPushButton("Generate and Test", self)
        self.run_button.setObjectName("generate-and-test")

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Project", self))
        row.addWidget(project_widget, 1)
        row.addWidget(folder_button)
        layout.addLayout(row)

        layout.addWidget(QLabel("Documents", self))
        layout.addWidget(self.document_list, 1)
        layout.addLayout(documents_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode", self))
        mode_row.addWidget(self.mode)
        layout.addLayout(mode_row)
        layout.addWidget(self.circuit_widget)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Use profile", self))
        profile_row.addWidget(self.use_profile, 1)
        layout.addLayout(profile_row)

        identity_row = QHBoxLayout()
        identity_row.addWidget(QLabel("Identity", self))
        identity_row.addWidget(self.part_manufacturer, 1)
        identity_row.addWidget(self.part_ordering_code, 1)
        layout.addLayout(identity_row)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Scope", self))
        scope_row.addWidget(self.scope)
        layout.addLayout(scope_row)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Provider", self))
        provider_row.addWidget(self.provider)
        layout.addLayout(provider_row)

        layout.addWidget(self.allow_remote)
        layout.addWidget(self.run_button)
        self._mode_changed(self.mode.currentText())

    # ------------------------------------------------------------------ state

    def project_dir(self) -> Path | None:
        text = self.project_edit.text().strip()
        return Path(text) if text else None

    def document_paths(self) -> list[Path]:
        return [
            Path(self.document_list.item(row).text()) for row in range(self.document_list.count())
        ]

    def circuit_file(self) -> Path | None:
        text = self.circuit_edit.text().strip()
        return Path(text) if text else None

    def use_profile_text(self) -> str:
        return self.use_profile.currentText().strip() or DEFAULT_USE_PROFILE

    def scope_text(self) -> str | None:
        text = self.scope.currentText()
        return None if text == "(all)" else text

    def provider_text(self) -> str | None:
        text = self.provider.currentText()
        return None if text == "(default)" else text

    def remote_allowed(self) -> bool:
        return self.allow_remote.isChecked()

    def identity(self) -> dict[str, str] | None:
        """Optional part identity, or None when no field was filled in."""
        manufacturer = self.part_manufacturer.text().strip()
        ordering_code = self.part_ordering_code.text().strip()
        if not manufacturer and not ordering_code:
            return None
        identity = {"manufacturer": manufacturer, "ordering_code": ordering_code}
        if ordering_code:
            identity["base_part"] = ordering_code
        return {key: value for key, value in identity.items() if value}

    # ------------------------------------------------------------------ filling

    def set_project(self, root: Path, config: Any | None = None) -> None:
        self.project_edit.setText(str(root))
        self.document_list.clear()
        if config is not None:
            part = getattr(config, "part", None)
            if part is not None:
                self.part_manufacturer.setText(str(getattr(part, "manufacturer", "") or ""))
                self.part_ordering_code.setText(str(getattr(part, "ordering_code", "") or ""))
            self.mode.setCurrentText(str(getattr(config, "mode", "component")))
            self.use_profile.setCurrentText(
                str(getattr(config, "use_profile", DEFAULT_USE_PROFILE))
            )
            for relative in getattr(config, "documents", []) or []:
                candidate = Path(relative)
                if not candidate.is_absolute():
                    candidate = root / relative
                if candidate.is_file():
                    self.document_list.addItem(QListWidgetItem(str(candidate)))
        circuit = self._pick_circuit(root)
        if circuit is not None:
            self.circuit_edit.setText(str(circuit))

    def _pick_circuit(self, root: Path) -> Path | None:
        circuit_dir = root / "circuit"
        for pattern in ("*.asc", "*.cir", "*.net"):
            matches = sorted(circuit_dir.glob(pattern)) if circuit_dir.is_dir() else []
            if matches:
                return matches[0]
        return None

    # ------------------------------------------------------------------ browsing

    def _browse_project(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Open project directory")
        if chosen:
            self.project_edit.setText(chosen)

    def _add_documents(self) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(
            self, "Add documents", "", "Documents (*.pdf *.txt *.md);;All files (*)"
        )
        for path in chosen:
            self.document_list.addItem(QListWidgetItem(path))

    def _remove_documents(self) -> None:
        for item in self.document_list.selectedItems():
            self.document_list.takeItem(self.document_list.row(item))

    def _browse_circuit(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select circuit", "", "Circuits (*.asc *.cir *.net);;All files (*)"
        )
        if chosen:
            self.circuit_edit.setText(chosen)

    def _mode_changed(self, mode: str) -> None:
        self.circuit_widget.setEnabled(mode == "circuit")


class MainWindow(QMainWindow):
    """BoardModeler desktop window; renders worker events, never verdicts."""

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        config_file: Path | None = None,
        worker_factory: Callable[[], WorkerClient] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_file = Path(config_file) if config_file is not None else config_path()
        self._config = config if config is not None else load_config(self._config_file)
        self._worker_factory = worker_factory
        self._client: WorkerClient | None = None
        self._project: Project | None = None
        self._stage_rows: list[dict[str, Any]] = []
        self._waveform_refs: list[str] = []
        self._pending_export_dir: Path | None = None
        self.last_result_event: dict[str, Any] | None = None
        self.last_error_event: dict[str, Any] | None = None

        self.setWindowTitle("Spice Maker")

        # ------------------------------------------------------------- widgets
        self.inputs = InputsPanel(self)
        self.inputs.run_button.clicked.connect(self.start_run)

        self.stage_list_widget = QListWidget(self)
        self.stage_list_widget.setObjectName("stage-list")
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.stage_list_widget.setFont(font)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 10)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("idle", self)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_run)

        stage_box = QGroupBox("Stages", self)
        stage_layout = QVBoxLayout(stage_box)
        stage_layout.addWidget(self.stage_list_widget, 1)
        stage_layout.addWidget(self.progress_bar)
        stage_layout.addWidget(self.progress_label)
        stage_layout.addWidget(self.cancel_button)

        left = QSplitter(Qt.Orientation.Vertical, self)
        left.addWidget(self.inputs)
        left.addWidget(stage_box)
        left.setStretchFactor(1, 1)

        self.results_panel = ResultsPanel(self)
        self.review = ReviewPanel(parent=self)
        self.review.set_project_dir(None)
        self.waveforms = WaveformView(self)
        self.waveform_selector = QComboBox(self)
        load_waveform = QPushButton("Load", self)
        load_waveform.clicked.connect(self._load_selected_waveform)
        waveform_page = QWidget(self)
        waveform_layout = QVBoxLayout(waveform_page)
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Waveform", waveform_page))
        selector_row.addWidget(self.waveform_selector, 1)
        selector_row.addWidget(load_waveform)
        waveform_layout.addLayout(selector_row)
        waveform_layout.addWidget(self.waveforms, 1)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.results_panel, "Results")
        self.tabs.addTab(self.review, "Review")
        self.tabs.addTab(waveform_page, "Waveforms")

        self.log = QTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFont(font)
        self.log.setObjectName("run-log")
        log_page = QWidget(self)
        log_layout = QVBoxLayout(log_page)
        log_layout.addWidget(self.log)
        self.tabs.addTab(log_page, "Log")

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.tabs)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        self._build_actions()
        self.statusBar().showMessage("ready")

    # ------------------------------------------------------------------ actions

    def _build_actions(self) -> None:
        self.action_open = QAction("Open project...", self)
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.triggered.connect(self._open_project_dialog)

        self.action_run = QAction("Generate and Test", self)
        self.action_run.setShortcut("F5")
        self.action_run.triggered.connect(self.start_run)

        self.action_cancel = QAction("Cancel", self)
        self.action_cancel.triggered.connect(self.cancel_run)
        self.action_cancel.setEnabled(False)

        self.action_export = QAction("Export...", self)
        self.action_export.triggered.connect(self._export_dialog)

        self.action_open_ltspice = QAction("Open in LTspice", self)
        self.action_open_ltspice.triggered.connect(self.open_in_ltspice)

        self.action_settings = QAction("Settings...", self)
        self.action_settings.triggered.connect(self.open_settings)

        self.action_installer = QAction("Setup...", self)
        self.action_installer.triggered.connect(self.open_installer)

        self.action_quit = QAction("Quit", self)
        self.action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.action_quit.triggered.connect(self.close)

        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.action_settings)
        tools_menu.addAction(self.action_installer)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)
        run_menu = self.menuBar().addMenu("&Run")
        run_menu.addAction(self.action_run)
        run_menu.addAction(self.action_cancel)
        run_menu.addAction(self.action_open_ltspice)

        toolbar = QToolBar("main", self)
        toolbar.addAction(self.action_open)
        toolbar.addAction(self.action_run)
        toolbar.addAction(self.action_cancel)
        toolbar.addAction(self.action_export)
        toolbar.addAction(self.action_open_ltspice)
        toolbar.addAction(self.action_settings)
        self.addToolBar(toolbar)

    # ------------------------------------------------------------------ project

    def load_project(self, directory: str | Path) -> bool:
        path = Path(directory)
        try:
            project = Project(path)
        except ProjectError as exc:
            self._say(f"cannot open project: {exc}")
            return False
        self._project = project
        self.inputs.set_project(project.root, project.config)
        self.setWindowTitle(f"Spice Maker - {project.config.name} [{project.config.project_id}]")
        self.review.set_project_dir(project.root)
        self._waveform_refs = []
        self.waveform_selector.clear()
        self._say(f"project loaded: {project.root}")
        return True

    def project_dir(self) -> Path | None:
        if self._project is not None:
            return self._project.root
        return self.inputs.project_dir()

    def _open_project_dialog(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Open project directory")
        if chosen:
            self.load_project(chosen)

    # ------------------------------------------------------------------ running

    def request_payload(self, *, export_dir: Path | None = None) -> dict[str, Any]:
        """The ``PipelineRequest`` fields for the inputs as they stand."""
        project = self.project_dir()
        payload: dict[str, Any] = {
            "project_dir": str(project) if project else "",
            "mode": self.inputs.mode.currentText(),
            "document_paths": [str(path) for path in self.inputs.document_paths()],
            "use_profile": self.inputs.use_profile_text(),
            "allow_remote": self.inputs.remote_allowed(),
            "max_repair_iterations": int(self._config.max_repair_iterations),
        }
        circuit = self.inputs.circuit_file()
        if circuit is not None and str(circuit) not in payload["document_paths"]:
            payload["document_paths"].append(str(circuit))
        identity = self.inputs.identity()
        if identity is not None:
            payload["part_identity"] = identity
        if self.inputs.provider_text():
            payload["provider"] = self.inputs.provider_text()
        if self.inputs.scope_text():
            payload["scope"] = self.inputs.scope_text()
        if export_dir is not None:
            payload["export_dir"] = str(export_dir)
        return payload

    def start_run(self, *, export_dir: Path | None = None) -> bool:
        project = self.project_dir()
        if project is None or not project.is_dir():
            self._say("select a project directory first")
            return False
        if self._client is not None and self._client.is_running():
            self._say("a run is already in progress")
            return False
        self._pending_export_dir = Path(export_dir) if export_dir is not None else None
        payload = self.request_payload(export_dir=self._pending_export_dir)
        self._reset_run_views(str(export_dir) if export_dir is not None else "run")
        client = self._ensure_client()
        try:
            request_path = client.start(payload, project_dir=project)
        except Exception as exc:
            self._say(f"could not start the worker: {type(exc).__name__}: {exc}")
            return False
        self._set_running(True)
        self._say(f"run started ({request_path.name})")
        return True

    def cancel_run(self) -> bool:
        if self._client is None or not self._client.is_running():
            self._say("nothing to cancel")
            return False
        cancelled = bool(self._client.cancel())
        if cancelled:
            self._say("cancelling: terminating the worker process tree")
        return cancelled

    def export_project(self, out_dir: str | Path | None = None) -> bool:
        project = self.project_dir()
        if project is None:
            self._say("select a project directory first")
            return False
        target = Path(out_dir) if out_dir is not None else project / "export"
        return self.start_run(export_dir=target)

    def _export_dialog(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Export to directory")
        if chosen:
            self.export_project(chosen)

    def open_in_ltspice(self) -> bool:
        target = self._deck_path()
        if target is None:
            self._say("no circuit file (.asc/.cir/.net) to open")
            return False
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self._say(f"open in LTspice: {target} ({'handed to the desktop' if opened else 'failed'})")
        return bool(opened)

    def _deck_path(self) -> Path | None:
        circuit = self.inputs.circuit_file()
        if circuit is not None and circuit.is_file():
            return circuit
        project = self.project_dir()
        if project is None:
            return None
        for directory in (project / "circuit", project):
            if not directory.is_dir():
                continue
            for pattern in ("*.asc", "*.cir", "*.net"):
                matches = sorted(directory.glob(pattern))
                if matches:
                    return matches[0]
        return None

    def open_settings(self) -> bool:
        from boardmodeler.ui.settings import SettingsDialog

        dialog = SettingsDialog(config=self._config, config_file=self._config_file, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._config = load_config(self._config_file)
            self._say(f"settings saved to {self._config_file}")
            return True
        return False

    def open_installer(self) -> int:
        from boardmodeler.ui.setup_dialog import SetupDialog

        wizard = SetupDialog(self)
        return int(wizard.exec())

    # ------------------------------------------------------------------ worker

    def _ensure_client(self) -> WorkerClient:
        if self._client is None:
            factory = self._worker_factory
            self._client = factory() if factory is not None else WorkerClient()
            self._connect(self._client)
        return self._client

    def _connect(self, client: WorkerClient) -> None:
        client.stage.connect(self._on_stage)
        client.progress.connect(self._on_progress)
        client.findings.connect(self._on_findings)
        client.review.connect(self._on_review)
        client.waveform.connect(self._on_waveform)
        client.result.connect(self._on_result)
        client.error.connect(self._on_error)
        client.stderr_text.connect(self._on_stderr)
        client.exited.connect(self._on_exited)

    def _set_running(self, running: bool) -> None:
        self.cancel_button.setEnabled(running)
        self.action_cancel.setEnabled(running)
        self.inputs.run_button.setEnabled(not running)
        self.action_run.setEnabled(not running)

    def _reset_run_views(self, label: str) -> None:
        self._stage_rows = []
        self.stage_list_widget.clear()
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"{label}: starting")
        self.results_panel.summary.setText("run in progress...")
        self.results_panel.details.clear()
        self.last_result_event = None
        self.last_error_event = None
        self._append_log(f"--- {label} ---")

    # ------------------------------------------------------------- event handlers

    def _on_stage(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage", ""))
        status = str(event.get("status", ""))
        detail = str(event.get("detail", ""))
        text = f"{stage:<18} {status:<15} {float(event.get('elapsed_s', 0.0)):>6.2f}s  {detail}"
        index = next(
            (position for position, row in enumerate(self._stage_rows) if row["stage"] == stage),
            None,
        )
        if index is None:
            index = len(self._stage_rows)
            self._stage_rows.append({"stage": stage, "status": status, "detail": detail})
            self.stage_list_widget.addItem(QListWidgetItem(text))
        else:
            self._stage_rows[index].update(status=status, detail=detail)
            self.stage_list_widget.item(index).setText(text)
        item = self.stage_list_widget.item(index)
        item.setForeground(QColor(STATUS_COLOURS.get(status, "#ffffff")))
        artifacts = event.get("artifacts") or []
        if artifacts:
            item.setToolTip("artifacts: " + ", ".join(str(a) for a in artifacts))
        self.progress_label.setText(f"{stage}: {status}")

    def _on_progress(self, event: dict[str, Any]) -> None:
        total = int(event.get("total", 0) or 0)
        done = int(event.get("done", 0) or 0)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(done, total))
        detail = str(event.get("detail", ""))
        if detail:
            self.progress_label.setText(f"{event.get('stage', '')}: {detail}")

    def _on_findings(self, event: dict[str, Any]) -> None:
        self.results_panel.set_findings(event.get("findings") or [])

    def _on_review(self, event: dict[str, Any]) -> None:
        self.review.set_items(event.get("items") or [])
        unresolved = len(self.review.unresolved_items())
        if unresolved:
            self._say(f"{unresolved} review item(s) need a decision")

    def _on_waveform(self, event: dict[str, Any]) -> None:
        ref = str(event.get("ref", ""))
        if not ref or ref in self._waveform_refs:
            return
        self._waveform_refs.append(ref)
        self.waveform_selector.addItem(ref)
        signals = ", ".join(str(s) for s in event.get("signals") or [])
        self._append_log(f"waveform {ref}" + (f" [{signals}]" if signals else ""))
        for violation in event.get("violations") or []:
            try:
                self.waveforms.add_violation_marker(
                    str(violation.get("req_id", "")), float(violation.get("t_s", 0.0))
                )
            except TypeError, ValueError:
                continue
        if len(self._waveform_refs) == 1:
            self.waveform_selector.setCurrentText(ref)
            self._load_selected_waveform()

    def _on_result(self, event: dict[str, Any]) -> None:
        self.last_result_event = event
        self.results_panel.set_result_event(event)
        summary = ", ".join(f"{k}={v}" for k, v in (event.get("summary") or {}).items())
        self._say(f"run finished: {event.get('status', '')} ({summary})")
        self._append_log(
            f"result: status={event.get('status')} summary={event.get('summary')} "
            f"export={event.get('export_dir')}"
        )

    def _on_error(self, event: dict[str, Any]) -> None:
        self.last_error_event = event
        self._say(f"error [{event.get('code')}]: {event.get('detail')}")
        self._append_log(f"error: {event.get('code')} - {event.get('detail')}")

    def _on_stderr(self, text: str) -> None:
        self._append_log(f"stderr: {text}")

    def _on_exited(self, code: int) -> None:
        self._set_running(False)
        outcome = self._client.outcome if self._client is not None else None
        cancelled = bool(outcome is not None and outcome.cancelled)
        state = "cancelled" if cancelled else "finished"
        self._say(f"worker {state} with exit code {code}")
        self._append_log(f"worker exit={code}")
        if code != 0 and not cancelled:
            self._say("the request could not be served; see the Log tab for stderr")

    # ------------------------------------------------------------------ accessors

    def results_table(self) -> Any:
        """The results table model (one row per ``TestResult``)."""
        return self.results_panel.results_model

    def findings_list(self) -> Any:
        """The findings table model, most critical first."""
        return self.results_panel.findings_model

    def stage_list(self) -> QListWidget:
        return self.stage_list_widget

    def review_panel(self) -> ReviewPanel:
        """The review panel (ambiguities, conflicts, missing evidence)."""
        return self.review

    def waveform_view(self) -> WaveformView:
        """The waveform view (custom QPainter widget)."""
        return self.waveforms

    def worker_client(self) -> WorkerClient | None:
        """The client running the current job, or None before the first run."""
        return self._client

    def stage_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._stage_rows]

    def waveform_refs(self) -> list[str]:
        return list(self._waveform_refs)

    def export_target(self) -> Path | None:
        """Directory the next export writes to, when an export was requested."""
        return self._pending_export_dir

    # ------------------------------------------------------------------ helpers

    def _load_selected_waveform(self) -> bool:
        ref = self.waveform_selector.currentText().strip()
        if not ref:
            return False
        path = Path(ref)
        if not path.is_absolute():
            project = self.project_dir()
            path = (project / ref) if project is not None else path
        if not path.is_file():
            self._say(f"waveform not found: {path}")
            return False
        size = path.stat().st_size
        if size > MAX_WAVEFORM_BYTES:
            self._say(
                f"{path.name} is {size / 1024 / 1024:.0f} MiB, above the "
                f"{MAX_WAVEFORM_BYTES // 1024 // 1024} MiB in-window limit; the file is intact"
            )
            return False
        try:
            names = self.waveforms.load_raw(path)
        except Exception as exc:
            self._say(f"cannot read waveform {path}: {type(exc).__name__}: {exc}")
            return False
        self._say(f"loaded {len(names)} trace(s) from {path.name}")
        return True

    def _append_log(self, text: str) -> None:
        self.log.append(text)

    def _say(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self._append_log(message)

    def closeEvent(self, event: Any) -> None:
        if self._client is not None and self._client.is_running():
            self._client.cancel()
        super().closeEvent(event)
