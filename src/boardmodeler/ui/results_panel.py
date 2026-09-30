"""Results panel: test results, findings (critical first), and run details.

The panel renders worker events verbatim — it never derives a verdict. Results
and findings carry the status the worker reported; the panel only sorts findings
so the most critical rows are on top and exposes the run's coverage/evidence/
assumption diagnostics in an expandable tree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

__all__ = [
    "FINDING_SEVERITY",
    "STATUS_COLOURS",
    "FindingsTableModel",
    "ResultsPanel",
    "ResultsTableModel",
    "sort_findings",
]

STATUS_COLOURS: dict[str, str] = {
    "PASS": "#2e7d32",
    "FAIL": "#b71c1c",
    "UNKNOWN": "#a1680b",
    "BLOCKED": "#6a4fa3",
    "NOT_APPLICABLE": "#546e7a",
}

FINDING_SEVERITY: dict[str, int] = {
    "FAIL": 0,
    "UNKNOWN": 1,
    "BLOCKED": 2,
    "PASS": 3,
    "NOT_APPLICABLE": 4,
}

_DETAIL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Coverage", ("coverage", "covered", "test_counts")),
    ("Evidence", ("evidence", "citation", "source")),
    ("Assumptions", ("assumption", "assumptions", "limitation", "limitations")),
)


def _status_colour(status: str) -> QColor | None:
    colour = STATUS_COLOURS.get(status)
    return QColor(colour) if colour else None


class ResultsTableModel(QAbstractTableModel):
    """One row per :class:`TestResult` the worker emitted."""

    COLUMNS = ("Test ID", "Status", "Expected", "Measured", "Detail", "Run")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    # Qt plumbing
    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self.COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell(row, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == 1:
            return _status_colour(str(row.get("status", "")))
        if role == Qt.ItemDataRole.FontRole and column == 1:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.get("detail") or row.get("expected") or ""
        return None

    def _cell(self, row: Mapping[str, Any], column: int) -> str:
        if column == 0:
            return str(row.get("test_id", ""))
        if column == 1:
            return str(row.get("status", ""))
        if column == 2:
            return str(row.get("expected", ""))
        if column == 3:
            measured = row.get("measured") or {}
            if isinstance(measured, Mapping) and measured:
                return ", ".join(f"{key}={value}" for key, value in measured.items())
            return ""
        if column == 4:
            detail = str(row.get("detail", ""))
            reason = row.get("unknown_reason") or row.get("blocked_reason")
            return f"{detail} [{reason}]" if reason else detail
        return str(row.get("run_id", ""))

    # data access for the window and for tests
    def set_results(self, results: Sequence[Mapping[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [dict(item) for item in results]
        self.endResetModel()

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def result_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None

    def statuses(self) -> list[str]:
        return [str(row.get("status", "")) for row in self._rows]


class FindingsTableModel(QAbstractTableModel):
    """Findings, most critical first (FAIL, UNKNOWN, BLOCKED, PASS, N/A)."""

    COLUMNS = ("Code", "Status", "Refdes", "Nets", "Message")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self.COLUMNS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell(row, index.column())
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return _status_colour(str(row.get("status", "")))
        if role == Qt.ItemDataRole.ToolTipRole:
            detail = row.get("detail") or {}
            return (
                "\n".join(f"{k}={v}" for k, v in detail.items()) if detail else row.get("message")
            )
        return None

    def _cell(self, row: Mapping[str, Any], column: int) -> str:
        if column == 0:
            return str(row.get("code", ""))
        if column == 1:
            return str(row.get("status", ""))
        if column == 2:
            refdes = row.get("refdes")
            return "" if refdes is None else str(refdes)
        if column == 3:
            return ", ".join(str(net) for net in row.get("nets") or [])
        return str(row.get("message", ""))

    def set_findings(self, findings: Sequence[Mapping[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = sort_findings(findings)
        self.endResetModel()

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def sort_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Critical first, then by refdes/code for a stable, scannable order."""
    return sorted(
        (dict(item) for item in findings),
        key=lambda item: (
            FINDING_SEVERITY.get(str(item.get("status", "")), 9),
            str(item.get("refdes") or ""),
            str(item.get("code", "")),
        ),
    )


class ResultsPanel(QWidget):
    """Results table + findings table + expandable run details."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = QLabel("no run yet", self)
        self.summary.setObjectName("results-summary")

        self.table = QTableView(self)
        self.table.setObjectName("results-table")
        self.results_model = ResultsTableModel(self.table)
        self.table.setModel(self.results_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setStyleSheet("QTableView { font-family: Consolas, monospace; }")

        self.findings_label = QLabel("Findings (most critical first): none", self)
        self.findings_table = QTableView(self)
        self.findings_table.setObjectName("findings-table")
        self.findings_model = FindingsTableModel(self.findings_table)
        self.findings_table.setModel(self.findings_model)
        self.findings_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.findings_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.findings_table.setStyleSheet("QTableView { font-family: Consolas, monospace; }")

        self.details = QTreeWidget(self)
        self.details.setObjectName("run-details")
        self.details.setHeaderLabels(["Run details", "value"])
        self.details.setStyleSheet("QTreeWidget { font-family: Consolas, monospace; }")

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 3)
        layout.addWidget(self.findings_label)
        layout.addWidget(self.findings_table, 2)
        layout.addWidget(self.details, 2)

    # ------------------------------------------------------------------ events

    def set_result_event(self, event: Mapping[str, Any]) -> None:
        """Render a worker ``result`` event."""
        results = event.get("results") or []
        self.results_model.set_results(results)
        for column in (0, 1, 2):
            self.table.resizeColumnToContents(column)
        summary = event.get("summary") or {}
        counts = ", ".join(f"{key}={value}" for key, value in summary.items()) or "no results"
        self.summary.setText(f"status: {event.get('status', '')} ({counts})")
        if event.get("findings"):
            self.set_findings(event["findings"])
        self._fill_details(event)

    def set_findings(self, findings: Sequence[Mapping[str, Any]]) -> None:
        self.findings_model.set_findings(findings)
        row_count = self.findings_model.rowCount()
        critical = sum(1 for row in self.findings_model.rows() if row.get("status") == "FAIL")
        self.findings_label.setText(
            f"Findings (most critical first): {row_count} total, {critical} FAIL"
            if row_count
            else "Findings (most critical first): none"
        )
        for column in (0, 1, 2):
            self.findings_table.resizeColumnToContents(column)

    def _fill_details(self, event: Mapping[str, Any]) -> None:
        self.details.clear()
        diagnostics = event.get("diagnostics") or {}
        grouped: dict[str, list[tuple[str, str]]] = {name: [] for name, _ in _DETAIL_GROUPS}
        grouped["Diagnostics"] = []
        for key, value in dict(diagnostics).items():
            target = "Diagnostics"
            lowered = str(key).lower()
            for name, prefixes in _DETAIL_GROUPS:
                if any(prefix in lowered for prefix in prefixes):
                    target = name
                    break
            grouped[target].append((str(key), str(value)))
        for name, _ in _DETAIL_GROUPS:
            items = grouped[name]
            node = QTreeWidgetItem([name, f"{len(items)} item(s)"])
            for key, value in items:
                node.addChild(QTreeWidgetItem([key, value]))
            self.details.addTopLevelItem(node)
        other = QTreeWidgetItem(["Diagnostics", f"{len(grouped['Diagnostics'])} item(s)"])
        for key, value in grouped["Diagnostics"]:
            other.addChild(QTreeWidgetItem([key, value]))
        self.details.addTopLevelItem(other)

        artifacts = QTreeWidgetItem(["Artifacts", f"{len(event.get('artifacts') or [])} item(s)"])
        for path in event.get("artifacts") or []:
            artifacts.addChild(QTreeWidgetItem([str(path), ""]))
        if event.get("manifest"):
            artifacts.addChild(QTreeWidgetItem(["manifest", str(event["manifest"])]))
        if event.get("export_dir"):
            artifacts.addChild(QTreeWidgetItem(["export", str(event["export_dir"])]))
        self.details.addTopLevelItem(artifacts)
        self.details.expandToDepth(0)

    def detail_group_names(self) -> list[str]:
        return [
            self.details.topLevelItem(index).text(0)
            for index in range(self.details.topLevelItemCount())
        ]
