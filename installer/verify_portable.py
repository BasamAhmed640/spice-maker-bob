"""Exercise the shipped installer: local environment, launchers, update, two copies.

The two copies are installed *concurrently* into different roots and then compared: each
must have its own interpreter in its own folder, must read its own configuration, and
must leave the other copy byte-identical. Nothing here writes outside ``build/`` except
the app's own folder, and no check may be reported unless it was observed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
import uuid
import winreg
from pathlib import Path

from verify_gui import verify

INSTALL_TIMEOUT = 900
SETUP = "Boardmodeler.cmd"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_version(repo: Path) -> str:
    """The version the sources declare, so no release check hard-codes one.

    The frozen application's window title carries ``boardmodeler.__version__``, so
    comparing it with this value is what proves the shipped app is the built source
    rather than an older freeze that happened to sit in ``dist/``.
    """
    return str(
        tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    )


def _json(text: str, what: str) -> dict:
    """Parse a child's JSON, failing with the raw text when it printed something else."""
    try:
        return json.loads(text)
    except ValueError as error:
        raise RuntimeError(f"{what} did not print JSON ({error}): {text[:400]}") from error


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name.upper().startswith(("PYTHON", "QT_", "QML", "VIRTUAL_ENV", "PIP_")):
            env.pop(name)
    return env


def _install(folder: Path) -> None:
    subprocess.run(
        [str(folder / "Install.exe"), "--silent", "--no-launch"],
        check=True,
        timeout=INSTALL_TIMEOUT,
    )


def _install_together(folders: tuple[Path, Path]) -> None:
    """Both roots at once: separate copies must not share a lock or any state."""
    processes = [
        (folder, subprocess.Popen([str(folder / "Install.exe"), "--silent", "--no-launch"]))
        for folder in folders
    ]
    for folder, process in processes:
        if process.wait(timeout=INSTALL_TIMEOUT) != 0:
            raise RuntimeError(f"Install.exe failed in {folder}")


def _snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    """Content hashes and mtimes, so a copy's files can be compared without holding them."""
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            relative = path.relative_to(folder).as_posix()
            files[relative] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return files


def _venv_python(root: Path) -> Path:
    return root / ".venv/Scripts/python.exe"


def _environment_report(root: Path) -> dict[str, object]:
    """Everything the copy's own environment must be, read back from disk."""
    python = _venv_python(root)
    assert python.is_file(), f"no environment interpreter in {root}"
    config = (root / ".venv/pyvenv.cfg").read_text()
    home = next(
        line.split("=", 1)[1].strip() for line in config.splitlines() if line.startswith("home")
    )
    assert Path(home) == root / "env/python", home
    assert Path(home).is_relative_to(root), home
    assert "AppData" not in config.replace(str(root), ""), config
    env = _clean_environment()
    env["SPICE_MAKER_ROOT"] = str(root) + os.sep
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import json, sys, numpy, pydantic, pypdf, pypdfium2, boardmodeler;"
            " from boardmodeler.config import config_path;"
            " print(json.dumps({'prefix': sys.prefix, 'base': sys.base_prefix,"
            " 'version': boardmodeler.__version__, 'numpy': numpy.__version__,"
            " 'config_path': str(config_path())}))",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    payload = _json(probe.stdout, "the copy's interpreter")
    assert Path(payload["prefix"]) == root / ".venv", payload
    assert Path(payload["base"]) == root / "env/python", payload
    # The launcher runs the same package the interpreter reports, from this folder only.
    cli = subprocess.run(
        # By full path: with NoDefaultCurrentDirectoryInExePath set, cmd will not look in cwd.
        ["cmd", "/c", str(root / SETUP), "version"],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=120,
    )
    assert cli.returncode == 0, cli.stderr
    assert str(payload["version"]) in cli.stdout, cli.stdout
    settings = {"config_path": payload["config_path"]}
    assert Path(settings["config_path"]).is_relative_to(root), settings
    return {
        "python": str(python),
        "home": home,
        "version": payload["version"],
        "numpy": payload["numpy"],
        "config_path": settings["config_path"],
        "wheels": len(list((root / "env/wheels").glob("*.whl"))),
    }


def _launchers(root: Path) -> dict[str, object]:
    for name in ("Start.cmd", SETUP, "Spice Maker.lnk"):
        assert (root / name).is_file(), f"{name} is missing from {root}"
    read = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "$sh=New-Object -ComObject WScript.Shell;"
            "$l=$sh.CreateShortcut([IO.Path]::Combine($env:ROOT,'Spice Maker.lnk'));"
            "Write-Output $l.TargetPath; Write-Output $l.WorkingDirectory",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, ROOT=str(root)),
        timeout=120,
    )
    assert read.returncode == 0, read.stderr
    target, working = read.stdout.strip().splitlines()
    assert Path(target) == root / "Start.cmd", target
    assert Path(working) == root, working
    return {"target": target, "working_directory": working}


_SHORTCUT_PLACES = (
    Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu",
    Path(os.environ["USERPROFILE"]) / "Desktop",
)
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def _outside_state() -> dict[str, list[str]]:
    """What Windows' own stores hold right now, read without assuming a clean machine."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        uninstall = sorted(
            winreg.EnumKey(key, index) for index in range(winreg.QueryInfoKey(key)[0])
        )
    appdata = sorted(
        str(path)
        for path in (
            Path(os.environ["APPDATA"]) / "Spice Maker",
            Path(os.environ["LOCALAPPDATA"]) / "Spice Maker",
        )
        if path.exists()
    )
    shortcuts = sorted(str(link) for place in _SHORTCUT_PLACES for link in place.rglob("*.lnk"))
    return {"uninstall": uninstall, "appdata": appdata, "shortcuts": shortcuts}


def _names_root(link: str, root: Path) -> bool:
    try:
        return str(root).lower().encode() in Path(link).read_bytes().lower().replace(b"\x00", b"")
    except OSError:
        return False


def _outside_entries(root: Path, before: dict[str, list[str]]) -> dict[str, object]:
    """Setup must add nothing to Windows' stores, compared with what was already there."""
    after = _outside_state()
    added = {
        name: [value for value in values if value not in before[name]]
        for name, values in after.items()
    }
    referencing = [link for link in after["shortcuts"] if _names_root(link, root)]
    assert not added["uninstall"], added
    assert not added["appdata"], added
    assert not added["shortcuts"], added
    assert not referencing, referencing
    return {
        "added_by_setup": added,
        "shortcuts_referencing_this_copy": referencing,
        "pre_existing_uninstall_entries": [
            name for name in before["uninstall"] if "spice" in name.lower()
        ],
    }


def run(repo: Path) -> dict:
    work = repo / "build" / ("portable-check-" + uuid.uuid4().hex[:8])
    outside_before = _outside_state()
    first, second = work / "copy-one", work / "copy-two"
    for folder in (first, second):
        folder.mkdir(parents=True)
        shutil.copy2(repo / "Install.exe", folder / "Install.exe")
    _install_together((first, second))
    exe = first / "app/SpiceMaker.exe"
    assert _sha256(exe) == _sha256(repo / "dist/SpiceMaker/SpiceMaker.exe")
    # Observed before any command runs in either copy: setup creates no user state at all.
    assert not (first / "data").exists(), "setup created data/ before first launch"
    assert not (second / "data").exists(), "the second copy got data/ from setup"
    assert not (first / ".venv-setup-error.txt").exists()
    assert not (second / ".venv-setup-error.txt").exists()
    environment = _environment_report(first)
    environment_two = _environment_report(second)
    assert environment["home"] != environment_two["home"]
    launchers = _launchers(first)
    outside = _outside_entries(first, outside_before)

    startup = verify(exe, repo / "build/portable-first-launch.png")
    assert "setup" in str(startup["title"]).lower(), startup
    data = first / "data"
    data.mkdir(exist_ok=True)
    config = {"setup_complete": True, "default_model_dir": "models", "web_reinforcement": False}
    (data / "config.json").write_text(json.dumps(config))
    # Test-owned ciphertext sentinel checks installer byte preservation; never a real key.
    (data / "credentials.bin").write_bytes(b"test-owned-encrypted-file-sentinel")
    before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
    snapshot = _snapshot(first)

    # Updating one copy may replace its own app/ and env/ and nothing else.
    _install(first)
    assert all((data / name).read_bytes() == value for name, value in before.items())
    after_update = _snapshot(first)
    for relative, value in snapshot.items():
        if relative.startswith(("data/", ".venv/")):
            assert after_update.get(relative) == value, f"update touched {relative}"

    # The second copy installs into its own root, and the first stays exactly as it was.
    # Nothing at all runs in the first root between these two snapshots.
    _install(second)
    stable = _snapshot(first)
    for relative, value in after_update.items():
        assert stable.get(relative) == value, f"the second copy changed {relative}"
    assert _environment_report(first)["config_path"] == environment["config_path"]
    assert _environment_report(second)["config_path"] == environment_two["config_path"]
    assert not (second / "data/credentials.bin").exists()
    assert _venv_python(first).is_file() and _venv_python(second).is_file()
    main = verify(exe, repo / "build/portable-main-window.png")
    version = _declared_version(repo)
    assert version in str(main["title"]), main["title"]
    env = _clean_environment()
    env["BOARDMODELER_CONFIG"] = str(data / "config.json")
    check = subprocess.run(
        [str(second / "app/SpiceMaker.exe"), "--cli", "setup", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    payload = _json(check.stdout, "the frozen CLI")
    assert Path(payload["config_path"]).is_relative_to(second)
    assert payload["model_dir"] is None
    report = {
        "status": "PASS",
        "version": version,
        "first_launch": startup["title"],
        "relaunch": main["title"],
        "same_folder_update_preserves_data": True,
        "fresh_copy_has_no_saved_settings_or_key": True,
        "ignores_external_config": True,
        "environment": environment,
        "second_environment": environment_two,
        "launchers": launchers,
        "two_copies_installed_concurrently": True,
        "second_copy_left_the_first_unchanged": True,
        "outside_the_folder": outside,
        "root": str(first),
        "installer_sha256": _sha256(repo / "Install.exe"),
        "executable_sha256": _sha256(exe),
        "installer_bytes": (repo / "Install.exe").stat().st_size,
    }
    (repo / "build/portable-verification.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    print(json.dumps(run(Path(__file__).resolve().parents[1]), indent=2))
