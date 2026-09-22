"""Vendor the environment this copy provisions into its own folder at install time.

The portable installer creates ``<root>/.venv`` from what this step lays down:

* ``env/python/`` - a pruned CPython runtime, so the target machine needs no Python
  of its own and nothing is ever fetched from a package index;
* ``env/wheels/`` - the pinned wheel set for the command line surface, plus the
  project wheel itself;
* ``env/requirements.txt`` and ``env/wheels.json`` - the exact pins and their
  SHA256, so the installer can verify what it installs.

The Qt wheels are deliberately absent: ``app/`` already carries the frozen GUI with
its own Python, and adding ``PySide6-Essentials`` (~77 MB compressed) would push the
committed ``Install.exe`` past GitHub's 100 MB per-file limit. The venv therefore
serves the CLI/scripting surface; the GUI starts from ``app/``. See
``installer/README.md``.

Build time only. This step may use the network (``uv export``, ``uv build`` and
``pip download``); the installer never does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__all__ = ["build", "main"]

# Runtime trees that are not part of a working interpreter we ship.
DROP_TOP_LEVEL = {
    "Doc",
    "NEWS.txt",
    "Scripts",
    "Tools",
    "include",
    "libs",
    "tcl",
}
# ``test`` is 34 MB of the stdlib, the rest are interpreters/GUI toolkits.
DROP_LIB = {
    "__pycache__",
    "idlelib",
    "pydoc_data",
    "site-packages",
    "test",
    "tkinter",
    "turtledemo",
}
# Tcl/Tk ships as DLLs beside the extension modules; nothing we vendor imports them.
DROP_DLLS = {
    "_testcapi.pyd",
    "_testinternalcapi.pyd",
    "_testmultiphase.pyd",
    "_testsinglephase.pyd",
    "_tkinter.pyd",
    "tcl86t.dll",
    "tk86t.dll",
}
# Loaded on demand from a base prefix that ``Lib/venv`` resolves through pyvenv.cfg.
KEEP_IN_BASE = {"python.exe", "pythonw.exe", "python3.dll", "python314.dll"}
# The GUI wheels. See the module docstring: the frozen app owns the GUI.
GUI_DISTRIBUTIONS = ("pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6")


def _keeps(relative: str) -> bool:
    """Whether a runtime file is part of the environment we vendor."""
    head, _, tail = relative.partition("/")
    if head in DROP_TOP_LEVEL:
        return False
    if head == "Lib":
        return tail.split("/", 1)[0] not in DROP_LIB
    if head == "DLLs":
        return tail not in DROP_DLLS
    return True


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def copy_runtime(base: Path, dest: Path) -> dict[str, int]:
    """Copy the interpreter, minus the parts we do not ship. Returns the size summary."""
    if not (base / "python.exe").is_file():
        raise RuntimeError(f"{base} does not look like a Windows Python installation")
    for required in ("Lib/venv/__init__.py", "Lib/ensurepip/__init__.py"):
        if not (base / required).is_file():
            raise RuntimeError(f"{required} is missing from {base}; the venv step needs it")
    files = kept = 0
    for source in sorted(base.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(base).as_posix()
        if not _keeps(relative):
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files += 1
        kept += source.stat().st_size
    return {"runtime_files": files, "runtime_bytes": kept}


def _marker_matches(marker: str) -> bool:
    """Whether an exported marker holds on this Windows x64 build target.

    The wheel set is Windows x64 only, so a marker that names another platform drops
    the pin and a matching marker keeps it. Without ``packaging`` every pin is kept and
    pip fails loudly if one of them has no Windows wheel.
    """
    try:
        from packaging.markers import Marker, default_environment
    except ImportError:  # pragma: no cover - packaging ships with the build venv
        return True
    environment = {
        key: value for key, value in default_environment().items() if isinstance(value, str)
    }
    return bool(Marker(marker).evaluate(environment))


def _evaluate(line: str) -> str | None:
    """One exported requirement as an exact pin for this Windows target, or None."""
    line = line.strip()
    if not line or line.startswith(("#", "-", "--")):
        return None
    body, _, marker = line.partition(";")
    name, separator, version = body.strip().partition("==")
    version = version.strip()
    if not separator or not version or name.strip().lower() in GUI_DISTRIBUTIONS:
        return None
    if marker.strip() and not _marker_matches(marker):
        return None
    return f"{name.strip()}=={version}"


def _project_version(repo: Path) -> str:
    import tomllib

    return str(
        tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    )


def requirements(repo: Path, exported: Path, pins_path: Path, version: str) -> list[str]:
    """The frozen app's runtime closure, minus the GUI wheels, as exact pins.

    ``exported`` is the install-time file the installer feeds to pip; it also pins the
    application wheel itself, which the wheelhouse carries. ``pins_path`` is only the
    external downloads, so the download step never asks an index for our own project.
    """
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(exported),
        ],
        cwd=repo,
    )
    pins = [
        pin
        for pin in (_evaluate(line) for line in exported.read_text(encoding="utf-8").splitlines())
        if pin
    ]
    if not pins:
        raise RuntimeError("uv export produced no pins")
    pins_path.write_text("\n".join(pins) + "\n", encoding="utf-8")
    exported.write_text("\n".join([*pins, f"boardmodeler=={version}"]) + "\n", encoding="utf-8")
    return pins


def download_wheels(runtime: Path, wheels: Path, requirements_file: Path) -> None:
    """Fetch the pinned wheels once, at build time, through a throwaway venv.

    ``Lib/venv`` and ``Lib/ensurepip`` are part of the vendored runtime, so the
    throwaway venv seeds pip from the wheel that ships inside CPython itself.
    """
    with tempfile.TemporaryDirectory(prefix="spice-maker-wheelhouse-") as raw:
        scratch = Path(raw)
        venv = scratch / "venv"
        _run([str(runtime / "python.exe"), "-m", "venv", str(venv)], cwd=scratch)
        seed = venv / "Scripts" / "python.exe"
        _run(
            [
                str(seed),
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--no-cache-dir",
                "--dest",
                str(wheels),
                "--requirement",
                str(requirements_file),
            ],
            cwd=scratch,
        )


def project_wheel(repo: Path, wheels: Path) -> None:
    """The application itself, so ``python -m boardmodeler.cli`` works in the venv."""
    _run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=repo)


def _manifest(wheels: Path) -> dict[str, object]:
    entries: dict[str, dict[str, object]] = {}
    total = 0
    for wheel in sorted(wheels.glob("*.whl")):
        size = wheel.stat().st_size
        entries[wheel.name] = {"sha256": _digest(wheel), "bytes": size}
        total += size
    return {"wheels": entries, "wheel_count": len(entries), "wheel_bytes": total}


def checksums(wheels: Path) -> Path:
    """The ``sha256sum`` file the installer verifies before it installs anything."""
    lines = [f"{_digest(wheel)}  {wheel.name}" for wheel in sorted(wheels.glob("*.whl"))]
    target = wheels.parent / "wheels.sha256"
    target.write_text("\n".join(lines) + "\n", encoding="ascii")
    return target


def build(repo: Path) -> dict[str, object]:
    """Assemble ``build/portable/env`` and return what it contains."""
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    env = repo / "build" / "portable" / "env"
    wheels = env / "wheels"
    if env.exists():
        try:
            shutil.rmtree(env)
        except OSError as error:  # pragma: no cover - a locked build tree is a failure
            raise RuntimeError(f"cannot clear {env}: {error}") from error
    wheels.mkdir(parents=True)
    summary: dict[str, object] = {"runtime_source": str(base), "env": str(env)}
    summary.update(copy_runtime(base, env / "python"))
    version = _project_version(repo)
    pins = requirements(
        repo, env / "requirements.txt", repo / "build" / "portable" / "pins.txt", version
    )
    summary["pins"] = len(pins)
    summary["version"] = version
    download_wheels(env / "python", wheels, repo / "build" / "portable" / "pins.txt")
    project_wheel(repo, wheels)
    summary["checksums"] = str(checksums(wheels))
    summary.update(_manifest(wheels))
    (env / "wheels.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    print(json.dumps(build(args.repo.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
