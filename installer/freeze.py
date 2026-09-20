"""Freeze with only Python and Windows on PATH, never unrelated native tool DLLs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    windows = Path(env["SYSTEMROOT"])
    paths = [Path(sys.executable).parent, Path(sys.base_prefix), windows / "System32", windows]
    env["PATH"] = os.pathsep.join(dict.fromkeys(map(str, paths)))
    for name in list(env):
        if name.upper().startswith(("PYTHON", "QT_", "QML")):
            env.pop(name)
    return env


if __name__ == "__main__":
    installer = Path(__file__).resolve().parent
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                "-I",
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                str(installer / "SpiceMaker.spec"),
            ],
            cwd=installer.parent,
            env=build_environment(),
        )
    )
