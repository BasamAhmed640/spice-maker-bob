"""Settings dialog: LTspice, provider, data policy and credentials.

The dialog writes configuration only through ``config.save_config`` and writes
secrets only into the folder's plain local credential file (``security.credentials.set_credential``); the
API key never appears in the config file, in a log line, or in ``values()``.
The smoke test button runs the same ``simulation.ltspice.smoke_test`` the CLI
uses and displays exactly what it observed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from boardmodeler.config import AppConfig, config_path, load_config, save_config
from boardmodeler.security.credentials import set_credential
from boardmodeler.simulation.ltspice import locate_outcome, smoke_test

__all__ = ["SettingsDialog"]

_PROVIDER_NAMES = ("fixture", "http_inference", "bob_direct", "bob_shell")


class SettingsDialog(QDialog):
    """Edits an :class:`AppConfig`; nothing is written until ``save()``."""

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        config_file: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Spice Maker settings")
        self._config_path = Path(config_file) if config_file is not None else config_path()
        self._config = config if config is not None else load_config(self._config_path)
        self._last_smoke: dict[str, Any] | None = None
        self._last_credential: str = "no credential stored in this session"

        # ------------------------------------------------------------ ltspice
        ltspice = self._config.ltspice
        self.ltspice_path = QLineEdit(ltspice.path or "", self)
        self.ltspice_path.setPlaceholderText("Choose LTspice.exe explicitly")
        browse = QPushButton("Browse...", self)
        browse.clicked.connect(self._browse_ltspice)
        row = QHBoxLayout()
        row.addWidget(self.ltspice_path)
        row.addWidget(browse)
        path_row = QWidget(self)
        path_row.setLayout(row)

        self.timeout = QDoubleSpinBox(self)
        self.timeout.setRange(1.0, 3600.0)
        self.timeout.setValue(float(ltspice.timeout_s))
        self.timeout.setSuffix(" s")
        self.lib_dir = QLineEdit(ltspice.lib_dir or "", self)
        self.lib_dir.setPlaceholderText("(installation library directory)")

        self.smoke_button = QPushButton("Run smoke test", self)
        self.smoke_button.clicked.connect(self.run_smoke_test)
        self.smoke_label = QLabel("smoke test not run in this session", self)
        self.smoke_label.setWordWrap(True)
        self.smoke_label.setObjectName("smoke-result")

        ltspice_form = QFormLayout()
        ltspice_form.addRow("LTspice executable", path_row)
        ltspice_form.addRow("Run timeout", self.timeout)
        ltspice_form.addRow("Library directory", self.lib_dir)
        ltspice_form.addRow(self.smoke_button, self.smoke_label)
        ltspice_box = QGroupBox("LTspice", self)
        ltspice_box.setLayout(ltspice_form)

        # ------------------------------------------------------------ provider
        self.provider = QComboBox(self)
        self.provider.addItems(sorted(self._config.providers))
        self.provider.currentTextChanged.connect(self._provider_changed)
        self.endpoint = QLineEdit(self)
        self.endpoint.setPlaceholderText("https://... (verified vendor endpoint)")
        self.model_name = QLineEdit(self)
        self.model_name.setPlaceholderText("model id from vendor documentation")
        self.secret = QLineEdit(self)
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("API key (saved in this folder as a plain local file)")
        store = QPushButton("SAVE KEY", self)
        store.clicked.connect(self.store_credential)
        self.credential_label = QLabel(self._last_credential, self)
        self.credential_label.setWordWrap(True)

        provider_form = QFormLayout()
        provider_form.addRow("Provider", self.provider)
        provider_form.addRow("Endpoint", self.endpoint)
        provider_form.addRow("Model", self.model_name)
        provider_form.addRow("API key", self.secret)
        provider_form.addRow(store, self.credential_label)
        provider_box = QGroupBox("Provider", self)
        provider_box.setLayout(provider_form)
        self._provider_changed(self.provider.currentText())

        # ------------------------------------------------------------- policy
        policy = self._config.data_policy
        self.allow_remote = QCheckBox("Allow remote inference (documents must permit it)", self)
        self.allow_remote.setChecked(bool(policy.allow_remote))
        self.deny_unknown = QCheckBox("Refuse documents with unknown classification", self)
        self.deny_unknown.setChecked(bool(policy.deny_unknown_classification))
        self.allow_bob_shell = QCheckBox("Allow Bob Shell (non-interactive tool execution)", self)
        self.allow_bob_shell.setChecked(bool(policy.allow_bob_shell))
        self.cache_extraction = QCheckBox("Cache extraction by prompt hash", self)
        self.cache_extraction.setChecked(bool(policy.cache_extraction))

        policy_layout = QVBoxLayout()
        policy_layout.addWidget(self.allow_remote)
        policy_layout.addWidget(self.deny_unknown)
        policy_layout.addWidget(self.allow_bob_shell)
        policy_layout.addWidget(self.cache_extraction)
        self.policy_summary = QLabel(policy.describe(), self)
        policy_layout.addWidget(self.policy_summary)
        policy_box = QGroupBox("Data policy", self)
        policy_box.setLayout(policy_layout)

        # ------------------------------------------------------------ buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.buttons = buttons

        layout = QVBoxLayout(self)
        layout.addWidget(ltspice_box)
        layout.addWidget(provider_box)
        layout.addWidget(policy_box)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ state

    def selected_provider_name(self) -> str:
        return str(self.provider.currentText())

    def _provider_changed(self, name: str) -> None:
        provider_config = self._config.providers.get(name)
        if provider_config is None:
            return
        self.endpoint.setText(provider_config.endpoint or "")
        self.model_name.setText(provider_config.model or "")

    def values(self) -> dict[str, Any]:
        """Widget state for tests; never contains the secret's value."""
        return {
            "config_path": str(self._config_path),
            "ltspice_path": self.ltspice_path.text().strip(),
            "timeout_s": self.timeout.value(),
            "lib_dir": self.lib_dir.text().strip(),
            "provider": self.selected_provider_name(),
            "endpoint": self.endpoint.text().strip(),
            "model": self.model_name.text().strip(),
            "secret_entered": bool(self.secret.text()),
            "allow_remote": self.allow_remote.isChecked(),
            "deny_unknown": self.deny_unknown.isChecked(),
            "allow_bob_shell": self.allow_bob_shell.isChecked(),
            "cache_extraction": self.cache_extraction.isChecked(),
            "smoke": self._last_smoke,
            "credential": self._last_credential,
        }

    # ------------------------------------------------------------- ltspice

    def _browse_ltspice(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Locate LTspice", "", "LTspice (LTspice.exe)")
        if chosen:
            self.ltspice_path.setText(chosen)

    def run_smoke_test(self) -> dict[str, Any]:
        """Run the CLI's smoke test and display exactly what it observed."""
        explicit = self.ltspice_path.text().strip() or None
        outcome = locate_outcome(explicit)
        install = outcome.install
        if install is None:
            observed = {
                "status": "fail",
                "detail": (
                    "LTspice not found; probed: " + ", ".join(outcome.probed_paths)
                    if outcome.probed_paths
                    else "LTspice not found"
                ),
                "path": None,
                "version": None,
            }
        else:
            workdir = Path(tempfile.mkdtemp(prefix="boardmodeler-settings-smoke-"))
            result = smoke_test(install.path, workdir, timeout_s=float(self.timeout.value()))
            observed = {
                "status": result.status,
                "detail": result.detail,
                "path": str(install.path),
                "version": result.version,
                "measured_v": result.measured_v,
                "expected_v": result.expected_v,
            }
        self._last_smoke = observed
        if observed["status"] == "pass":
            self.smoke_label.setText(f"PASS - {observed['detail']}")
        else:
            self.smoke_label.setText(f"FAIL - {observed['detail']}")
        return observed

    # ------------------------------------------------------------- credential

    def credential_name(self) -> str:
        return self.selected_provider_name()

    def store_credential(self) -> str:
        """Write only the key itself to the app's plain local credential file."""
        secret = self.secret.text()
        if not secret:
            self._last_credential = "no secret entered; nothing stored"
            self.credential_label.setText(self._last_credential)
            return self._last_credential
        try:
            set_credential(self.credential_name(), secret)
        except Exception:
            self._last_credential = "credential NOT stored: could not write the local file"
        else:
            self._last_credential = "stored in this folder's local credential file"
            self.secret.clear()
        self.credential_label.setText(self._last_credential)
        return self._last_credential

    # ------------------------------------------------------------------ save

    def build_config(self) -> AppConfig:
        config = self._config.model_copy(deep=True)
        config.ltspice.path = self.ltspice_path.text().strip() or None
        config.ltspice.timeout_s = float(self.timeout.value())
        config.ltspice.lib_dir = self.lib_dir.text().strip() or None
        name = self.selected_provider_name()
        provider_config = config.providers.get(name)
        if provider_config is not None:
            provider_config.endpoint = self.endpoint.text().strip() or None
            provider_config.model = self.model_name.text().strip() or None
        config.data_policy.allow_remote = self.allow_remote.isChecked()
        config.data_policy.deny_unknown_classification = self.deny_unknown.isChecked()
        config.data_policy.allow_bob_shell = self.allow_bob_shell.isChecked()
        config.data_policy.cache_extraction = self.cache_extraction.isChecked()
        return config

    def save(self) -> Path:
        """Persist the edited configuration and return the written path."""
        self._config = self.build_config()
        written = save_config(self._config, self._config_path)
        self.policy_summary.setText(self._config.data_policy.describe())
        return written

    def accept(self) -> None:
        """Persist the edited configuration, then close the dialog."""
        self.save()
        super().accept()
