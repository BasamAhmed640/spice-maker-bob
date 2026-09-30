"""Review panel: ambiguities, conflicts and missing evidence in one place.

Accept / Defer / Reject decisions are recorded on the matching
``ReviewItem.resolution`` in the project's ``review.json``; the panel never
blocks the run (a resolution is applied to the file, the worker keeps going) and
never answers a question on the user's behalf.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

__all__ = ["ACTIONS", "ReviewPanel"]

ACTIONS: tuple[str, ...] = ("accepted", "deferred", "rejected")

_COLUMNS = ("ID", "Kind", "Blocking", "Question", "Resolution")


class ReviewPanel(QWidget):
    """Renders ``ReviewItem`` dicts and records a decision per item."""

    resolved = Signal(str, str)
    """``(item_id, action)`` — emitted after the decision was recorded."""

    def __init__(
        self,
        *,
        on_resolve: Callable[[str, str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_resolve = on_resolve
        self._items: list[dict[str, Any]] = []
        self._project_dir: Path | None = None

        self.heading = QLabel("Review: no open questions", self)
        self.table = QTableWidget(0, len(_COLUMNS), self)
        self.table.setObjectName("review-table")
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setStyleSheet("QTableWidget { font-family: Consolas, monospace; }")
        self.table.itemSelectionChanged.connect(self._show_selected)

        self.detail = QTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setStyleSheet("QTextEdit { font-family: Consolas, monospace; }")

        self.accept_button = QPushButton("ACCEPT", self)
        self.defer_button = QPushButton("DEFER", self)
        self.reject_button = QPushButton("REJECT", self)
        self.accept_button.clicked.connect(lambda: self.resolve_selected("accepted"))
        self.defer_button.clicked.connect(lambda: self.resolve_selected("deferred"))
        self.reject_button.clicked.connect(lambda: self.resolve_selected("rejected"))
        for button in (self.accept_button, self.defer_button, self.reject_button):
            button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.defer_button)
        buttons.addWidget(self.reject_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.table, 3)
        layout.addWidget(self.detail, 1)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ data

    def set_project_dir(self, path: str | Path | None) -> None:
        """Set the project whose ``review.json`` resolutions are written."""
        self._project_dir = Path(path) if path is not None else None
        self.reload()

    def project_dir(self) -> Path | None:
        return self._project_dir

    def review_path(self) -> Path | None:
        return None if self._project_dir is None else self._project_dir / "review.json"

    def set_items(self, items: Sequence[Mapping[str, Any]]) -> None:
        self._items = [dict(item) for item in items]
        self._render()

    def reload(self) -> list[dict[str, Any]]:
        """Load ``review.json`` when it exists; otherwise keep the current items."""
        path = self.review_path()
        if path is None or not path.is_file():
            self._render()
            return self.items()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return self.items()
        if isinstance(payload, list):
            self.set_items(payload)
        elif isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
            self.set_items(payload["items"])
        return self.items()

    def items(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items]

    def blocking_items(self) -> list[dict[str, Any]]:
        return [item for item in self.items() if item.get("blocking")]

    def unresolved_items(self) -> list[dict[str, Any]]:
        return [item for item in self.items() if not item.get("resolution")]

    # ------------------------------------------------------------------ acting

    def resolve(self, item_id: str, action: str) -> bool:
        """Record ``action`` for ``item_id``; returns False when it is unknown."""
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, not {action!r}")
        for item in self._items:
            if str(item.get("id")) == str(item_id):
                item["resolution"] = action
                self._write_back(str(item_id), action)
                self._render()
                self.resolved.emit(str(item_id), action)
                if self._on_resolve is not None:
                    self._on_resolve(str(item_id), action)
                return True
        return False

    def resolve_selected(self, action: str) -> bool:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return False
        return self.resolve(str(self._items[row].get("id")), action)

    def _write_back(self, item_id: str, action: str) -> bool:
        path = self.review_path()
        if path is None or not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return False
        if not isinstance(payload, list):
            return False
        changed = False
        for entry in payload:
            if isinstance(entry, dict) and str(entry.get("id")) == item_id:
                entry["resolution"] = action
                changed = True
        if not changed:
            return False
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True

    # ------------------------------------------------------------------ render

    def _render(self) -> None:
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            values = (
                str(item.get("id", "")),
                str(item.get("kind", "")),
                "yes" if item.get("blocking") else "no",
                str(item.get("question", "")),
                str(item.get("resolution") or "-"),
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 2 and item.get("blocking"):
                    cell.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row, column, cell)
        for column in range(len(_COLUMNS)):
            self.table.resizeColumnToContents(column)
        open_count = len(self.unresolved_items())
        self.heading.setText(
            f"Review: {len(self._items)} item(s), {open_count} unresolved"
            if self._items
            else "Review: no open questions"
        )
        self._update_buttons()
        self._show_selected()

    def _update_buttons(self) -> None:
        enabled = self.table.currentRow() >= 0
        for button in (self.accept_button, self.defer_button, self.reject_button):
            button.setEnabled(enabled)

    def _show_selected(self) -> None:
        self._update_buttons()
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            self.detail.clear()
            return
        item = self._items[row]
        lines = [
            f"id: {item.get('id', '')}",
            f"kind: {item.get('kind', '')}",
            f"blocking: {'yes' if item.get('blocking') else 'no'}",
            "",
            str(item.get("question", "")),
        ]
        options = item.get("options") or []
        if options:
            lines += ["", "options:"] + [f"  - {option}" for option in options]
        if item.get("recommended"):
            lines += ["", f"recommended: {item['recommended']}"]
        requirements = item.get("affected_requirement_ids") or []
        if requirements:
            lines += ["", "requirements: " + ", ".join(str(r) for r in requirements)]
        if item.get("resolution"):
            lines += ["", f"resolution: {item['resolution']}"]
        self.detail.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------ tests

    def row_count(self) -> int:
        return self.table.rowCount()

    def table_values(self) -> list[list[str]]:
        return [
            [
                self.table.item(row, column).text() if self.table.item(row, column) else ""
                for column in range(self.table.columnCount())
            ]
            for row in range(self.table.rowCount())
        ]
