"""BoardModeler desktop application (PySide6) — D12.

Importing this package must not require a display or a ``QApplication``: Qt is
imported inside the modules that need it. ``boardmodeler ui`` and
``boardmodeler setup`` reach :func:`boardmodeler.ui.app.main` and
:func:`boardmodeler.ui.setup_dialog.main` through these lazy wrappers.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the desktop application (INTERFACES §4)."""
    from boardmodeler.ui.app import main as app_main

    return app_main(argv)
