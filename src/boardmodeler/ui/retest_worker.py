"""The saved-file verifier runs off the GUI thread, without rebuilding a candidate."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QThread, Signal


class RetestWorker(QThread):
    progressed = Signal(object)
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, directory: Path, request=None, parent=None) -> None:
        super().__init__(parent)
        self.directory = directory
        self.request = request
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            from boardmodeler.cli import _run_model_test
            from boardmodeler.pipeline.make_model import RowOutcome

            self.progressed.emit(
                SimpleNamespace(
                    stage="judge",
                    status="running",
                    detail="Testing the saved files against fixed requirements",
                    counts={},
                )
            )
            payload, _ran = _run_model_test(
                self.directory,
                timeout_s=getattr(self.request, "timeout_s", 120.0),
                cancel=self._cancel,
            )
            subckt = payload.get("subckt", "")
            library = self.directory / f"{subckt}.lib"
            symbol = library.with_suffix(".asy")
            card = self.directory / "MODEL_CARD.md"
            self.finished_result.emit(
                SimpleNamespace(
                    status=payload["status"],
                    detail=payload.get("detail", ""),
                    counts=payload.get("counts", {}),
                    part=payload.get("part", ""),
                    out_dir=self.directory,
                    card_path=card if card.is_file() else None,
                    lib_path=None
                    if payload["status"] == "BLOCKED" or not library.is_file()
                    else library,
                    asy_path=None
                    if payload["status"] == "BLOCKED" or not symbol.is_file()
                    else symbol,
                    rows=tuple(RowOutcome(**row) for row in payload.get("rows", ())),
                    request=self.request,
                )
            )
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
