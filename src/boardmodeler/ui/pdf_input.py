"""A local PDF drop populates an input; it never moves the original or starts a build."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit


def readable_pdf(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return b"%PDF-" in stream.read(1024)
    except OSError:
        return False


def dropped_pdf(mime) -> Path | None:
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    path = Path(urls[0].toLocalFile())
    return path if readable_pdf(path) else None


class PdfPathEdit(QLineEdit):
    pdf_dropped = Signal(str)

    def dragEnterEvent(self, event) -> None:
        if (
            self.isEnabled()
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
            self.isEnabled()
            and event.possibleActions() & Qt.DropAction.CopyAction
            and path is not None
        ):
            self.pdf_dropped.emit(str(path))
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
