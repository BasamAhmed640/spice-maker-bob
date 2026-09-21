"""QApplication entry point (INTERFACES §4).

``boardmodeler ui [--project DIR] [--installer]``. Importing this module
requires Qt; importing :mod:`boardmodeler.ui` does not (the package facade
imports this lazily).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtWidgets import QApplication

from boardmodeler import __version__

__all__ = ["APPLICATION_NAME", "build_application", "main", "window_icon"]

APPLICATION_NAME = "Spice Maker"

#: The pepper mark rendered by ``installer/render_assets.py``. It is looked up next to
#: the frozen executable first (the installer ships it), then in the source tree.
_ICON_NAMES = ("pepper.ico", "icon.ico")


def window_icon() -> object | None:
    """The application icon, or ``None`` when this build has no asset for it."""
    from pathlib import Path

    from PySide6.QtGui import QIcon

    roots: list[Path] = []
    executable = getattr(sys, "executable", "")
    if executable:
        roots.append(Path(executable).resolve().parent)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(str(bundle)))
    roots.append(Path(__file__).resolve().parents[3] / "installer" / "assets")
    for root in roots:
        for name in _ICON_NAMES:
            candidate = root / name
            if candidate.is_file():
                icon = QIcon(str(candidate))
                if not icon.isNull():
                    return icon
    return None


def build_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the running ``QApplication`` or create one (never two)."""
    existing = QApplication.instance()
    app = existing if existing is not None else QApplication(list(argv) if argv is not None else [])
    from PySide6.QtCore import Qt

    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APPLICATION_NAME)
    icon = window_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    return app


def main(argv: Sequence[str] | None = None, *, exec_app: bool = True) -> int:
    """Launch the desktop application (or the installer with ``--installer``)."""
    parser = argparse.ArgumentParser(
        prog="boardmodeler ui", description="Spice Maker desktop application"
    )
    parser.add_argument("--project", type=Path, default=None, help="project directory to open")
    parser.add_argument(
        "--installer", action="store_true", help="open the setup page instead of the model maker"
    )
    parser.add_argument(
        "--board-ui",
        action="store_true",
        help="launch the earlier board/circuit window instead of the model maker",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    from boardmodeler.config import load_config
    from boardmodeler.storage import initialize, portable

    initialize()
    app = build_application([sys.argv[0]])
    if args.installer:
        from boardmodeler.ui.setup_dialog import SetupDialog

        if not exec_app:
            SetupDialog()
            return 0
        return int(SetupDialog().exec())

    if args.board_ui:
        from boardmodeler.ui.main_window import MainWindow

        window = MainWindow()
        if args.project is not None:
            window.load_project(args.project)
        window.show()
        if not exec_app:
            return 0
        return int(app.exec())

    from boardmodeler.ui.model_maker import ModelMakerWindow

    if portable() and not load_config().setup_complete and exec_app:
        from boardmodeler.ui.setup_dialog import SetupDialog

        SetupDialog().exec()
        if not load_config().setup_complete:
            return 0

    maker = ModelMakerWindow()
    maker.show()
    if not exec_app:
        return 0
    return int(app.exec())


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(main())
