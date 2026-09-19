"""Setup: the handful of settings that persist between sessions, on one page.

Everything a model build does not need to be asked again each time lives here and only
here: where LTspice is, which agent provider answers the API key, the key itself, the
folder finished models land in, the LTspice user library and whether the web is searched
for supporting material. The main window carries none of it.

The agent rows are built from :mod:`boardmodeler.agent_providers`: a build whose catalog
holds one provider shows no provider row at all and keeps that provider's own key label.
A configured provider this build does not accept is shown as such, in its own line, and
is never swapped for the default: SAVE leaves the configured id alone until the user
picks a provider here.

The page is sized to its content — no fixed-height frame with dead space under it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from boardmodeler import agent_providers
from boardmodeler.agent_providers import AgentProvider
from boardmodeler.config import AppConfig, config_path, load_config, save_config
from boardmodeler.ui.theme import CGA, RETRO_STYLESHEET

__all__ = ["SetupDialog", "configured_provider", "describe_settings", "ltspice_user_lib", "main"]

_HINT = f"color: {CGA['bright_cyan']}; font-family: Consolas; font-size: 9pt;"
_STATUS = f"color: {CGA['grey']}; font-family: Consolas; font-size: 9pt;"


def ltspice_user_lib(home: Path | None = None) -> Path:
    """The per-user LTspice library (never the installation directory)."""
    base = home if home is not None else Path.home()
    return base / "AppData" / "Local" / "LTspice" / "lib"


def configured_provider(config: AppConfig) -> tuple[AgentProvider | None, str]:
    """``(provider, reason)`` for ``config.agent_provider``; never another provider.

    An id this build does not accept comes back as ``None`` plus the same
    ``api_provider_unavailable`` text
    :func:`boardmodeler.authoring.backends.build_agent_backend` refuses with, so
    the page, the window and the engine cannot disagree about the refusal. An
    empty setting means this build's default provider.
    """
    wanted = str(config.agent_provider or "").strip()
    if not wanted:
        return agent_providers.default_provider(), ""
    provider = agent_providers.by_id(wanted)
    if provider is not None:
        return provider, ""
    accepted = ", ".join(repr(name) for name in agent_providers.ids()) or "none"
    return None, (
        f"api_provider_unavailable: {wanted!r} is not a provider this build accepts; "
        f"use one of {accepted}"
    )


def describe_settings(config: AppConfig) -> dict[str, object]:
    """The persisted settings as data, for ``boardmodeler setup --json`` and tests."""
    from boardmodeler.security.credentials import describe_credential

    provider, reason = configured_provider(config)
    shown = provider or agent_providers.default_provider()
    return {
        "config_path": str(config_path()),
        "ltspice_path": config.ltspice.path,
        "model_dir": config.default_model_dir,
        "web_reinforcement": config.web_reinforcement,
        "ltspice_user_lib": str(ltspice_user_lib()),
        # The id the config names, never a substitute; the flag says whether this
        # build accepts it, so a caller sees the refusal instead of another provider.
        "agent_provider": str(config.agent_provider or "").strip() or shown.id,
        "agent_provider_accepted": provider is not None,
        "agent_provider_problem": reason,
        "agent_model": config.agent_model or shown.model,
        "agent_api_key": describe_credential(shown.credential),
        "accepted_providers": list(agent_providers.ids()),
    }


class SetupDialog(QDialog):
    """One page of persistent settings; SAVE writes them, CLOSE discards nothing else."""

    def __init__(self, parent: object | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Spice Maker setup")
        self.setStyleSheet(RETRO_STYLESHEET)
        self._config = load_config()
        self._provider, self._provider_problem = configured_provider(self._config)
        #: The id SAVE must write, or ``None`` while the configured id is left alone.
        self._provider_choice: str | None = None if self._provider is None else self._provider.id
        if self._provider is None:
            # Something on this page must own the key row; the line below says whose
            # key it is *not*, and SAVE keeps the configured id until the user picks.
            self._provider = agent_providers.default_provider()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        layout.addLayout(grid)
        row = 0

        # --- LTspice ---------------------------------------------------------
        self.ltspice_edit = QLineEdit(self._resolved_ltspice())
        choose_exe = QPushButton("Choose…")
        choose_exe.clicked.connect(self._choose_ltspice)
        smoke = QPushButton("RUN SMOKE TEST")
        smoke.clicked.connect(self._run_smoke)
        grid.addWidget(QLabel("LTSPICE"), row, 0)
        grid.addWidget(self.ltspice_edit, row, 1)
        grid.addWidget(choose_exe, row, 2)
        grid.addWidget(smoke, row, 3)
        row += 1
        self.ltspice_status = QLabel("")
        self.ltspice_status.setStyleSheet(_STATUS)
        grid.addWidget(self.ltspice_status, row, 1, 1, 3)
        row += 1

        # --- the agent: which provider, and its API key ----------------------
        self.restricted_note: QLabel | None = None
        self.use_note: QPushButton | None = None
        only = agent_providers.only_provider()
        if only is not None:
            self.restricted_note = QLabel(f"{only.label} is the provider this build uses.")
            self.restricted_note.setStyleSheet(_HINT)
            grid.addWidget(self.restricted_note, row, 1, 1, 3)
            row += 1
            if self._provider_problem:
                # A config can name a provider this build has no transport for (another
                # build wrote it, or it was hand-edited). SAVE never substitutes on its
                # own, so the page offers the one provider it does have, by name.
                self.use_note = QPushButton(f"USE {only.label.upper()}")
                self.use_note.clicked.connect(self._accept_only_provider)
                grid.addWidget(self.use_note, row, 1, 1, 3)
                row += 1
        self.provider_combo: QComboBox | None = None
        if len(agent_providers.CATALOG) > 1:
            combo = QComboBox()
            for provider in agent_providers.CATALOG:
                combo.addItem(provider.label, provider.id)
            combo.setCurrentIndex(max(0, combo.findData(self._provider.id)))
            combo.currentIndexChanged.connect(self._on_provider_changed)
            self.provider_combo = combo
            grid.addWidget(QLabel("AGENT"), row, 0)
            grid.addWidget(combo, row, 1, 1, 3)
            row += 1

        self.provider_status: QLabel | None = None
        if self._provider_problem:
            guidance = (
                f"store the {only.label} API key this build uses"
                if only is not None
                else "pick a provider here and SAVE to replace it"
            )
            self.provider_status = QLabel(
                f"config names {str(self._config.agent_provider).strip()!r}, which this build "
                f"does not accept —\n{guidance}"
            )
            self.provider_status.setStyleSheet(_HINT)
            grid.addWidget(self.provider_status, row, 1, 1, 3)
            row += 1

        self.key_label = QLabel(self._provider.key_label)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(f"paste your {self._provider.label} API key")
        save_key = QPushButton("SAVE KEY")
        save_key.clicked.connect(self._save_key)
        grid.addWidget(self.key_label, row, 0)
        grid.addWidget(self.key_edit, row, 1)
        grid.addWidget(save_key, row, 2, 1, 2)
        row += 1
        self.key_status = QLabel("")
        self.key_status.setStyleSheet(_STATUS)
        grid.addWidget(self.key_status, row, 1, 1, 3)
        row += 1
        self.key_hint = QLabel("")
        self.key_hint.setStyleSheet(_HINT)
        self.key_hint.setMaximumWidth(620)
        grid.addWidget(self.key_hint, row, 1, 1, 3)
        row += 1
        #: The model id row: only providers that take one from this application show it.
        self.model_label = QLabel("MODEL")
        self.model_edit = QLineEdit()
        grid.addWidget(self.model_label, row, 0)
        grid.addWidget(self.model_edit, row, 1, 1, 3)

        # --- where models go -------------------------------------------------
        row += 1
        self.model_dir_edit = QLineEdit(
            self._config.default_model_dir or str(Path.home() / "Spice Maker")
        )
        choose_dir = QPushButton("Choose…")
        choose_dir.clicked.connect(self._choose_model_dir)
        grid.addWidget(QLabel("MODEL FOLDER"), row, 0)
        grid.addWidget(self.model_dir_edit, row, 1)
        grid.addWidget(choose_dir, row, 2, 1, 2)
        row += 1

        library = QLabel(str(ltspice_user_lib()))
        library.setStyleSheet(
            f"color: {CGA['bright_green']}; font-family: Consolas; font-size: 9pt;"
        )
        grid.addWidget(QLabel("LTSPICE LIBRARY"), row, 0)
        grid.addWidget(library, row, 1, 1, 3)
        row += 1

        self.reinforce_check = QCheckBox(
            "search the web for supporting material while making a model"
        )
        self.reinforce_check.setChecked(self._config.web_reinforcement)
        grid.addWidget(self.reinforce_check, row, 1, 1, 3)

        # --- actions ---------------------------------------------------------
        row = QHBoxLayout()
        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet(_STATUS)
        row.addWidget(self.saved_label, 1)
        save = QPushButton("SAVE")
        save.clicked.connect(self._save)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.close)
        row.addWidget(save)
        row.addWidget(close)
        layout.addLayout(row)

        self._show_provider(self._provider)
        self._refresh_status()
        self.adjustSize()
        self.setFixedSize(self.size())

    # ------------------------------------------------------------------ helpers
    def _resolved_ltspice(self) -> str:
        if self._config.ltspice.path:
            return self._config.ltspice.path
        from boardmodeler.simulation.ltspice import locate

        install = locate()
        return str(install.path) if install is not None else ""

    def _show_provider(self, provider: AgentProvider) -> None:
        """Point the key and model rows at ``provider`` without touching the config."""
        self._provider = provider
        self.key_label.setText(provider.key_label)
        self.key_edit.setPlaceholderText(f"paste your {provider.label} API key")
        self.key_hint.setText(f"{provider.key_hint}\n{provider.docs}")
        self.model_edit.setText(self._model_for(provider))
        self.model_label.setVisible(provider.model_editable)
        self.model_edit.setVisible(provider.model_editable)

    def _model_for(self, provider: AgentProvider) -> str:
        """The model shown for ``provider``: the stored one only if it is that provider's."""
        stored = (self._config.agent_model or "").strip()
        if stored and agent_providers.by_id(self._config.agent_provider) is provider:
            return stored
        return provider.model or ""

    def _on_provider_changed(self) -> None:
        assert self.provider_combo is not None  # only connected when the row exists
        provider = agent_providers.by_id(self.provider_combo.currentData())
        if provider is None:  # pragma: no cover - the combo only holds catalog ids
            return
        self._show_provider(provider)
        self._provider_choice = provider.id
        self._refresh_status()
        self.adjustSize()

    def _refresh_status(self) -> None:
        from boardmodeler.security.credentials import describe_credential

        provider = self._provider
        self.key_status.setText(f"stored key: {describe_credential(provider.credential)}")
        chosen = self._config.ltspice.path
        self.ltspice_status.setText(
            "using the path set here" if chosen else "path discovered automatically"
        )

    def _choose_ltspice(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Where is LTspice.exe?",
            self.ltspice_edit.text() or str(Path.home()),
            "LTspice (*.exe)",
        )
        if path:
            self.ltspice_edit.setText(path)

    def _choose_model_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Where should finished models be saved?", self.model_dir_edit.text()
        )
        if path:
            self.model_dir_edit.setText(path)

    def _run_smoke(self) -> None:
        self.ltspice_status.setText("running the smoke test…")
        self.repaint()
        try:
            from boardmodeler.simulation.ltspice import locate, smoke_test

            install = locate(self.ltspice_edit.text().strip() or None)
            if install is None:
                self.ltspice_status.setText("not found: nothing to smoke-test")
                return
            result = smoke_test(install.path, Path(tempfile.mkdtemp(prefix="bm-smoke-")))
        except Exception as exc:
            self.ltspice_status.setText(f"smoke test failed: {exc}")
            return
        measured = getattr(result, "measured_v", None)
        status = getattr(result, "status", "unknown")
        detail = getattr(result, "detail", "")
        if measured is None:
            self.ltspice_status.setText(f"{status}: {detail}"[:160])
        else:
            self.ltspice_status.setText(f"{status}: measured {measured:.4f} V (analytic 0.632 V)")

    def _save_key(self) -> None:
        value = self.key_edit.text().strip()
        if not value:
            QMessageBox.information(self, "Nothing to save", "Paste the key first.")
            return
        try:
            from boardmodeler.security.credentials import set_credential

            set_credential(self._provider.credential, value)
        except Exception as exc:
            QMessageBox.warning(self, "Could not store the key", str(exc))
            return
        self.key_edit.clear()
        self._refresh_status()
        self.saved_label.setText(
            f"{self._provider.label} key stored in the Windows credential store"
        )

    def _accept_only_provider(self) -> None:
        """Accept this build's one provider — the explicit fix for a config naming another.

        Nothing is substituted: the click is the user's choice, and SAVE is what writes it.
        """
        only = agent_providers.only_provider()
        if only is None:  # pragma: no cover - the button exists only for a single entry
            return
        self._provider = only
        self._provider_choice = only.id
        self.saved_label.setText(f"{only.label} will be used when you SAVE")

    def _save(self) -> None:
        self._config.ltspice.path = self.ltspice_edit.text().strip() or None
        self._config.default_model_dir = self.model_dir_edit.text().strip() or None
        self._config.web_reinforcement = self.reinforce_check.isChecked()
        if self._provider_choice is not None:
            self._config.agent_provider = self._provider_choice
            if self._provider.model_editable:
                self._config.agent_model = self.model_edit.text().strip() or None
        try:
            path = save_config(self._config)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.saved_label.setText(f"saved to {path}")


def main(argv: Sequence[str] | None = None) -> int:
    """``boardmodeler setup`` / ``boardmodeler ui --installer`` entry point."""
    parser = argparse.ArgumentParser(
        prog="boardmodeler setup",
        description=(
            "Persistent settings: LTspice path, agent provider and API key, model folder, "
            "web reinforcement"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="print the resolved settings instead of a window"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.json:
        print(json.dumps(describe_settings(load_config()), indent=2))
        return 0

    if not os.environ.get("QT_QPA_PLATFORM") and not sys.platform.startswith("win"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from boardmodeler.ui.app import build_application

    build_application([sys.argv[0]])
    return int(SetupDialog().exec())
