"""The build window's readiness lights and its VERIFY KEY & TOOLS button.

One coloured dot per :class:`boardmodeler.security.readiness.Check` — green ready,
amber usable with a limit, red a build would stop here, grey not tested yet — with the
check's own sentence as the tooltip. The local state is shown when the window opens
(nothing is sent); VERIFY runs :func:`~boardmodeler.security.readiness.verify_all` on a
worker thread and repaints the dots when it returns.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from boardmodeler.security.readiness import (
    Check,
    agent_light_label,
    local_checks,
    overall,
    verify_all,
)
from boardmodeler.ui.theme import CGA

__all__ = ["STATE_COLOURS", "ReadinessStrip"]

STATE_COLOURS: dict[str, str] = {
    "ok": CGA["bright_green"],
    "warn": CGA["yellow"],
    "fail": CGA["bright_red"],
    "unchecked": CGA["grey"],
}
_STATE_WORDS = {"ok": "ready", "warn": "limited", "fail": "problem", "unchecked": "not tested"}

#: A verification that has not answered by then is reported as such, not waited on.
VERIFY_TIMEOUT_S = 120.0


class ReadinessStrip(QWidget):
    """``● API KEY ● MODEL ● LTSPICE ● PDF ● OCR ● INTERNET  [VERIFY KEY & TOOLS]``."""

    def __init__(self, parent: QWidget | None = None, *, verify=verify_all, local=local_checks):
        super().__init__(parent)
        self._verify = verify
        self._local = local
        self._pending: tuple[threading.Event, list, float] | None = None
        self.checks: list[Check] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.lights: dict[str, QLabel] = {}
        for key, label in (
            ("key", "API KEY"),
            ("model", agent_light_label()),
            ("ltspice", "LTSPICE"),
            ("pdf", "PDF"),
            ("ocr", "OCR"),
            ("internet", "INTERNET"),
        ):
            light = QLabel(f"● {label}")
            light.setObjectName(f"ready_{key}")
            self.lights[key] = light
            row.addWidget(light)
        self.verify_button = QPushButton("VERIFY KEY && TOOLS")
        self.verify_button.setObjectName("verifyKeyButton")
        self.verify_button.setToolTip(
            "Sends one small request to your provider in the same shape a build uses (key, "
            "model id and reply format), then runs an LTspice smoke circuit. No datasheet or "
            "model text is sent; it may use a little API credit."
        )
        self.verify_button.clicked.connect(self.start_verify)
        row.addWidget(self.verify_button)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)
        self.show_checks(
            [
                Check(key, light.text()[2:], "unchecked", "checking…")
                for key, light in self.lights.items()
            ]
        )

    def state_of(self, key: str) -> str:
        return next((check.state for check in self.checks if check.key == key), "unchecked")

    def show_checks(self, checks: list[Check]) -> None:
        self.checks = list(checks)
        for check in checks:
            light = self.lights.get(check.key)
            if light is None:
                continue
            colour = STATE_COLOURS.get(check.state, CGA["grey"])
            light.setText(f"● {check.label}")
            light.setStyleSheet(f"color: {colour}; font-family: Consolas; font-size: 9pt;")
            light.setToolTip(
                f"{check.label}: {_STATE_WORDS.get(check.state, check.state)} — {check.detail}"
            )
        worst = overall(checks)
        self.verify_button.setToolTip(
            self.verify_button.toolTip().split("\n\nLast result:")[0]
            + f"\n\nLast result: {_STATE_WORDS.get(worst, worst)}"
        )

    def refresh_local(self) -> None:
        """Repaint from this machine's state alone (no request is sent)."""
        if self._pending is not None:
            return
        try:
            self.show_checks(self._local())
        except Exception as exc:  # pragma: no cover - a broken config must not block the window
            self.show_checks([Check("key", "API KEY", "fail", f"settings unreadable: {exc}")])

    def start_verify(self) -> None:
        if self._pending is not None:
            return
        cancel = threading.Event()
        results: list = []
        self._pending = (cancel, results, time.monotonic())
        self.verify_button.setEnabled(False)
        self.verify_button.setText("VERIFYING…")
        verify = self._verify

        def run() -> None:
            try:
                results.append(verify(cancel=cancel))
            except Exception:
                results.append(None)

        threading.Thread(target=run, name="readiness-check", daemon=True).start()
        self._timer.start()

    def _poll(self) -> None:
        if self._pending is None:
            self._timer.stop()
            return
        cancel, results, started = self._pending
        if not results and time.monotonic() - started < VERIFY_TIMEOUT_S:
            return
        cancel.set()
        self._timer.stop()
        self._pending = None
        self.verify_button.setEnabled(True)
        self.verify_button.setText("VERIFY KEY && TOOLS")
        checks = results[0] if results else None
        if checks is None:
            local = self._local()
            checks = [
                Check("key", "API KEY", "warn", "the check did not finish; try again")
                if check.key == "key"
                else check
                for check in local
            ]
        self.show_checks(checks)

    def cancel(self) -> None:
        if self._pending is not None:
            self._pending[0].set()
        self._pending = None
        self._timer.stop()
        self.verify_button.setEnabled(True)
        self.verify_button.setText("VERIFY KEY && TOOLS")
