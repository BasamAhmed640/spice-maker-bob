"""Standard-library Windows bootstrap for a source ZIP of Spice Maker.

Setup.cmd chooses a supported CPython before starting this file. This module owns
the local environment and Desktop shortcut, outside the application's write guard.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "requirements.txt"
SHORTCUT_STATE = ROOT / "data" / "shortcut.json"
SHORTCUT_NAME = "Spice Maker.lnk"


class SetupError(Exception):
    """A problem that the batch launcher can show without a Python traceback."""


def _use_utf8() -> None:
    """Keep pip and setup output usable from a Unicode extraction path."""
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode:
        raise SetupError(f"The previous step failed (exit {result.returncode}).")


def _check_folder() -> None:
    if len(str(ROOT)) > 200:
        raise SetupError(
            "This folder path is over 200 characters. Move the extracted folder "
            "near the drive root and run Setup.cmd again."
        )
    try:
        scratch = ROOT / "data" / "temp"
        if not scratch.resolve().is_relative_to(ROOT):
            raise SetupError("The data folder points outside this app folder.")
        if not (ROOT / ".venv").resolve().is_relative_to(ROOT):
            raise SetupError("The .venv folder points outside this app folder.")
        scratch.mkdir(parents=True, exist_ok=True)
        probe = scratch / ".write-check"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        tempfile.tempdir = str(scratch)
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(scratch)
    except OSError as exc:
        raise SetupError(
            "This folder is not writable. Extract the ZIP into a folder you can edit, "
            "then run Setup.cmd again."
        ) from exc


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["SPICE_MAKER_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PIP_CONFIG_FILE"] = os.devnull
    # A machine-level pip config or extra index must not add unpinned downloads.
    for name in tuple(env):
        if name.startswith("PIP_") and name not in {"PIP_CONFIG_FILE", "PIP_CERT"}:
            env.pop(name)
    return env


def _install_packages() -> None:
    if not REQUIREMENTS.is_file():
        raise SetupError("requirements.txt is missing. Download a fresh source ZIP.")
    wheel_dir = ROOT / "data" / "temp" / "wheels"
    # Never let an old, possibly altered wheel bypass the pre-install hash pass.
    if not wheel_dir.resolve().is_relative_to(ROOT):
        raise SetupError("The temporary wheel folder points outside this app folder.")
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    env = _environment()
    print("Package source: https://pypi.org/simple/ (HTTPS_PROXY is honored).", flush=True)
    print(
        "Downloading and checking every pinned wheel before installing any package...", flush=True
    )
    try:
        _run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--index-url",
                "https://pypi.org/simple/",
                "--dest",
                str(wheel_dir),
                "-r",
                str(REQUIREMENTS),
            ],
            env=env,
        )
        print("Installing the verified wheels into this folder's .venv...", flush=True)
        _run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-index",
                "--find-links",
                str(wheel_dir),
                "-r",
                str(REQUIREMENTS),
            ],
            env=env,
        )
    except SetupError as exc:
        raise SetupError(
            "Could not download or verify the pinned packages. Check Internet access "
            "and HTTPS_PROXY, or check whether requirements.txt was changed. " + str(exc)
        ) from exc


def _ensure_pip() -> None:
    probe = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "--version"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return
    print("Adding pip from Python's bundled ensurepip to this .venv...", flush=True)
    _run([str(VENV_PYTHON), "-m", "ensurepip", "--upgrade"])


def _check_venv() -> None:
    """Refuse a stale launcher that would install outside this copy's environment."""
    code = (
        "import pathlib,sys,sysconfig; "
        "ok=(sys.implementation.name=='cpython' and sys.version_info[:2]==(3,14) "
        "and pathlib.Path(sys.prefix).resolve()==pathlib.Path(sys.argv[1]).resolve() "
        "and sysconfig.get_platform()==sys.argv[2]); sys.exit(0 if ok else 1)"
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-I", "-c", code, str(ROOT / ".venv"), sysconfig.get_platform()],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise SetupError(
            "The existing .venv does not use this folder's supported Python 3.14. "
            "Move that .venv aside and run Setup.cmd again."
        )


def _powershell(script: str, *, extra_env: dict[str, str] | None = None) -> str:
    env = dict(os.environ)
    env.update(extra_env or {})
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode:
        raise SetupError("Windows could not update the Desktop shortcut.")
    return result.stdout.strip()


def _desktop() -> Path:
    value = _powershell("[Environment]::GetFolderPath('Desktop')")
    if not value:
        raise SetupError("Windows did not provide a Desktop folder.")
    return Path(value)


def _saved_shortcut() -> Path | None:
    if not SHORTCUT_STATE.is_file():
        return None
    try:
        saved = json.loads(SHORTCUT_STATE.read_text(encoding="utf-8"))
        path = Path(saved["path"])
        return path if path.name == SHORTCUT_NAME else None
    except OSError, ValueError, KeyError, TypeError:
        return None


def _remove_shortcut(override: Path | None = None) -> None:
    target = override / SHORTCUT_NAME if override else _saved_shortcut()
    if target is None:
        target = _desktop() / SHORTCUT_NAME
    if target.name != SHORTCUT_NAME:
        raise SetupError("Shortcut location is invalid.")
    try:
        target.unlink(missing_ok=True)
        SHORTCUT_STATE.unlink(missing_ok=True)
    except OSError as exc:
        raise SetupError("Windows could not remove the Spice Maker shortcut.") from exc
    print(f"Shortcut removed from {target.parent}.")


def _write_shortcut(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / SHORTCUT_NAME
    # WScript.Shell can convert characters outside the active ANSI code page when
    # it saves .lnk properties (for example Ω becomes O). Use the Unicode COM
    # interface directly, and pass all paths through environment variables.
    script = r"""
$source = @'
using System;
using System.Runtime.InteropServices;
namespace SpiceMaker {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IShellLinkW {
        void GetPath(IntPtr file, int maxPath, IntPtr findData, uint flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription(IntPtr name, int maxName);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory(IntPtr directory, int maxPath);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments(IntPtr args, int maxPath);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCmd);
        void SetShowCmd(int showCmd);
        void GetIconLocation(IntPtr iconPath, int maxPath, out int icon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int icon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    public static class Shortcut {
        public static void Save(string target, string workdir, string icon, string path) {
            Type type = Type.GetTypeFromCLSID(
                new Guid("00021401-0000-0000-C000-000000000046"), true);
            object link = Activator.CreateInstance(type);
            try {
                IShellLinkW unicode = (IShellLinkW)link;
                unicode.SetPath(target);
                unicode.SetWorkingDirectory(workdir);
                unicode.SetIconLocation(icon, 0);
                ((System.Runtime.InteropServices.ComTypes.IPersistFile)link).Save(path, true);
            } finally {
                Marshal.FinalReleaseComObject(link);
            }
        }
    }
}
'@
Add-Type -TypeDefinition $source -ErrorAction Stop
[SpiceMaker.Shortcut]::Save($env:SPICE_START_PATH, $env:SPICE_APP_ROOT,
    $env:SPICE_ICON_PATH, $env:SPICE_SHORTCUT_PATH)
"""
    _powershell(
        script,
        extra_env={
            "SPICE_SHORTCUT_PATH": str(target),
            "SPICE_START_PATH": str(ROOT / "Start.cmd"),
            "SPICE_APP_ROOT": str(ROOT),
            "SPICE_ICON_PATH": str(ROOT / "assets" / "pepper.ico"),
        },
    )
    SHORTCUT_STATE.parent.mkdir(parents=True, exist_ok=True)
    SHORTCUT_STATE.write_text(json.dumps({"path": str(target)}, indent=2), encoding="utf-8")
    print(f"Shortcut ready: {target}")


def _want_shortcut(choice: str | None, *, yes: bool) -> bool:
    if choice is not None:
        return choice == "yes"
    if _saved_shortcut() is not None:
        return True
    if yes or not sys.stdin.isatty():
        return False
    try:
        answer = input("Create a Desktop shortcut? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    _use_utf8()
    parser = argparse.ArgumentParser(description="Set up the local Spice Maker environment")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-install-python", action="store_true")
    parser.add_argument("--remove", action="store_true", help="remove the Desktop shortcut")
    parser.add_argument("--ltspice")
    parser.add_argument("--model-dir")
    parser.add_argument("--provider")
    parser.add_argument("--internet", choices=("on", "off"))
    parser.add_argument("--shortcut", choices=("yes", "no"))
    parser.add_argument("--shortcut-dir", type=Path)
    parser.add_argument("--key-env", metavar="NAME")
    args = parser.parse_args(argv)
    try:
        if args.remove:
            _remove_shortcut(args.shortcut_dir)
            return 0
        _check_folder()
        if not VENV_PYTHON.is_file():
            print("Creating .venv in the extracted folder...", flush=True)
            venv.EnvBuilder(with_pip=False).create(ROOT / ".venv")
        else:
            print("Using the existing .venv.", flush=True)
        _check_venv()
        _ensure_pip()
        _install_packages()
        wizard_args: list[str] = []
        for name in ("ltspice", "model_dir", "provider", "internet", "key_env"):
            value = getattr(args, name)
            if value is not None:
                wizard_args.extend(("--" + name.replace("_", "-"), value))
        if args.yes:
            wizard_args.append("--yes")
        print("Saving settings and checking LTspice...", flush=True)
        _run(
            [str(VENV_PYTHON), "-m", "boardmodeler.setup_wizard", *wizard_args],
            env=_environment(),
        )
        try:
            if _want_shortcut(args.shortcut, yes=args.yes):
                _write_shortcut(args.shortcut_dir or _desktop())
            elif args.shortcut == "no" and _saved_shortcut() is not None:
                _remove_shortcut()
        except (SetupError, OSError) as exc:
            print(f"Shortcut warning: {exc}", file=sys.stderr)
        print("Setup finished. Double-click Start.cmd to open the menu.")
        return 0
    except (OSError, SetupError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup could not finish: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
