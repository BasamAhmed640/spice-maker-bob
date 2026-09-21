"""Frozen entry point for the Spice Maker desktop application.

PyInstaller freezes *this* file into ``SpiceMaker.exe``. It adds no behaviour of its
own — it only picks between the two surfaces the package already has:

``SpiceMaker.exe``                    -> ``boardmodeler.ui.app.main([])`` (the model maker)
``SpiceMaker.exe --cli ...``          -> ``boardmodeler.cli.main([...])``
``SpiceMaker.exe -m boardmodeler.cli ...`` -> the same, for callers that re-enter the
frozen exe with the module form (``ui/model_maker.py`` spawns diagnostics that way, where
``sys.executable`` *is* ``SpiceMaker.exe``)

A ``--windowed`` PyInstaller build has no standard streams. ``--cli`` therefore rebinds
them, in order: the handles the caller passed (a pipe or a redirected file, which is how
the app's own CHECK ENVIRONMENT re-enters this exe), the console it was started from, or
``data/logs/cli.log`` — which it names inside that file.

The executable and all application-owned files stay beside Install.exe.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["APP_DIR_NAME", "app_dir", "cli_log_path", "main"]

APP_DIR_NAME = "SpiceMaker"
"""The portable edition name."""


def app_dir() -> Path:
    from boardmodeler.storage import data_dir

    return data_dir() / "logs"


def cli_log_path() -> Path:
    """Where ``--cli`` output goes when the process has no console."""
    return app_dir() / "cli.log"


def _reopen_inherited_handles() -> bool:
    """Reopen stdout/stderr on the standard handles this process inherited.

    ``ui/model_maker.py`` re-enters the frozen exe with ``capture_output=True`` — a pipe
    — so its output must go back through those handles, which is why this runs before the
    console attach: writing to a console would bypass the caller's pipe.
    """
    try:
        # These handles must outlive this function: they become the process streams.
        out = open(1, "w", encoding="utf-8", buffering=1, errors="replace", closefd=False)  # noqa: SIM115
        err = open(2, "w", encoding="utf-8", buffering=1, errors="replace", closefd=False)  # noqa: SIM115
    except OSError:
        return False
    sys.stdout, sys.stderr = out, err
    return True


def _attach_console() -> bool:
    """Reopen stdout/stderr on the console that started this process.

    A windowed executable does not inherit the parent's streams, so a shell that runs
    ``SpiceMaker.exe --cli ...`` sees nothing; attaching to the parent console makes the
    frozen exe behave like the console script it wraps.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return False
        # As above: the console handles live for the rest of the process.
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115
    except OSError:
        return False
    return True


def _cli_streams() -> Path | None:
    """Give a windowed build usable output; returns the log path when it had to log."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    for reconnect in (_reopen_inherited_handles, _attach_console):
        if reconnect():
            return None
    log = cli_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log, "w", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115
    sys.stdout = stream
    sys.stderr = stream
    return log


_CLI_PREFIXES = (("--cli",), ("-m", "boardmodeler.cli"))
"""How the CLI can be asked for: our own switch, or the module form a frozen build is
re-entered with (``sys.executable -m boardmodeler.cli ...`` is really
``SpiceMaker.exe -m boardmodeler.cli ...``)."""


def _cli_arguments(args: list[str]) -> list[str] | None:
    """The arguments after a CLI prefix, or ``None`` when this is a GUI launch."""
    for prefix in _CLI_PREFIXES:
        if tuple(args[: len(prefix)]) == prefix:
            return args[len(prefix) :]
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the GUI, or the CLI when the arguments select it."""
    args = list(sys.argv[1:] if argv is None else argv)
    from boardmodeler.storage import initialize, install_write_guard

    initialize()
    install_write_guard()
    forwarded = _cli_arguments(args)
    if forwarded is not None:
        log = _cli_streams()
        from boardmodeler.cli import main as cli_main

        code = cli_main(forwarded)
        if log is not None:
            print(f"output written to {log}")
        return int(code)
    from boardmodeler.ui.app import main as ui_main

    return int(ui_main([]))


if __name__ == "__main__":  # pragma: no cover - frozen entry point
    raise SystemExit(main())
