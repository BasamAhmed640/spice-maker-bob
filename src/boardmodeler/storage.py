"""All application-owned state belongs beside this copy of the installer."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def portable() -> bool:
    return bool(getattr(sys, "frozen", False) or os.environ.get("SPICE_MAKER_ROOT"))


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
        return folder.parent if folder.name == "app" else folder
    override = os.environ.get("SPICE_MAKER_ROOT")
    return Path(override).resolve() if override else Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return app_root() / "data"


def local_path(path: str | Path) -> Path:
    value = Path(path)
    resolved = (app_root() / value).resolve() if not value.is_absolute() else value.resolve()
    if portable() and not resolved.is_relative_to(app_root()):
        raise ValueError(f"Choose a folder inside {app_root()}; this copy keeps its data there.")
    return resolved


def model_dir(configured: str | None = None) -> Path:
    return local_path(configured) if configured else app_root() / "models"


def library_dir() -> Path:
    return app_root() / "library"


def initialize() -> None:
    """No user-profile fallback if the extracted folder is not writable."""
    scratch = data_dir() / "temp"
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scratch)
    os.environ["MPLCONFIGDIR"] = str(data_dir() / "plot-cache")
    if getattr(sys, "frozen", False):
        os.chdir(app_root())


def bob_environment(env: dict[str, str]) -> dict[str, str]:
    """Give the child its own local profile; never copy a global key or profile."""
    result = dict(env)
    if not portable():
        return result
    profile = data_dir() / "bob-profile"
    for key, folder in {
        "HOME": profile,
        "USERPROFILE": profile,
        "APPDATA": profile / "AppData" / "Roaming",
        "LOCALAPPDATA": profile / "AppData" / "Local",
        "XDG_CONFIG_HOME": profile / ".config",
        "XDG_CACHE_HOME": profile / ".cache",
    }.items():
        folder.mkdir(parents=True, exist_ok=True)
        result[key] = str(folder)
    for key in ("TEMP", "TMP", "TMPDIR"):
        result[key] = str(data_dir() / "temp")
    return result


def install_write_guard() -> None:
    """Refuse Python file mutations outside the portable root, including CLI exports."""
    if not getattr(sys, "frozen", False):
        return
    base = app_root()

    def check(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            candidate = Path(os.fsdecode(path)).resolve()
            if not candidate.is_relative_to(base):
                raise PermissionError(f"Spice Maker writes only inside {base}: {candidate}")

    def audit(event, args):
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                check(path)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            check(args[0])
        elif event in {"os.rename", "os.link"}:
            check(args[0])
            check(args[1])
        elif event == "os.symlink":
            check(args[1])
            check(args[0])

    sys.addaudithook(audit)
