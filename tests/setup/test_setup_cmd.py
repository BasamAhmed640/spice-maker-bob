"""The Python step can be exercised without running a real installer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Setup.cmd runs on Windows")
REPO = Path(__file__).resolve().parents[2]


def _host_pin_arch() -> str:
    architecture = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get(
        "PROCESSOR_ARCHITECTURE", ""
    )
    return "arm64" if architecture.lower() == "arm64" else "amd64"


def _require_native_python() -> None:
    expected = "win-arm64" if _host_pin_arch() == "arm64" else "win-amd64"
    if sysconfig.get_platform() != expected:
        pytest.skip("test needs a native CPython 3.14 for this Windows architecture")


def _copy_setup(tmp_path: Path) -> Path:
    app = tmp_path / "Spice Maker café"
    tools = app / "tools"
    tools.mkdir(parents=True)
    shutil.copyfile(REPO / "Setup.cmd", app / "Setup.cmd")
    shutil.copyfile(REPO / "tools" / "python-install-pins.txt", tools / "python-install-pins.txt")
    return app


def _run_setup(app: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(app / "Setup.cmd"), *args],
        input="",
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
        check=False,
    )


def _compile_stub(path: Path, source: str) -> None:
    env = os.environ.copy()
    env["SPICE_TEST_CS_SOURCE"] = source
    env["SPICE_TEST_EXE"] = str(path)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Add-Type -TypeDefinition $env:SPICE_TEST_CS_SOURCE "
            "-OutputAssembly $env:SPICE_TEST_EXE -OutputType ConsoleApplication",
        ],
        text=True,
        capture_output=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _test_registry_root() -> tuple[str, str]:
    relative = rf"Software\SpiceMakerSetupTests\{uuid.uuid4().hex}\PythonCore"
    return rf"HKCU\{relative}", relative


def _delete_test_registry(relative: str) -> None:
    import winreg

    for key in (
        rf"{relative}\3.14\InstallPath",
        rf"{relative}\3.14",
        relative,
        relative.rsplit("\\", 1)[0],
    ):
        with suppress(FileNotFoundError):
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, key, winreg.KEY_WOW64_64KEY)


def test_missing_python_exits_without_installing(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = (
        rf"HKCU\Software\SpiceMakerSetupTests\{uuid.uuid4().hex}"
    )

    result = _run_setup(app, env, "--yes", "--no-install-python")

    assert result.returncode != 0
    assert "Python 3.14 is required" in result.stdout
    assert not (app / "data").exists()


def test_unsupported_architecture_explains_problem(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    env = os.environ.copy()
    env.pop("PROCESSOR_ARCHITEW6432", None)
    env["PROCESSOR_ARCHITECTURE"] = "x86"

    result = _run_setup(app, env, "--yes")

    assert result.returncode != 0
    assert "Unsupported Windows architecture" in result.stdout
    assert not (app / "data").exists()


def test_long_folder_path_is_rejected_before_python_install(tmp_path: Path) -> None:
    app = tmp_path
    while len(str(app)) <= 201:
        app /= "nested-folder-" + "x" * 28
    app.mkdir(parents=True)
    shutil.copyfile(REPO / "Setup.cmd", app / "Setup.cmd")
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = (
        rf"HKCU\Software\SpiceMakerSetupTests\{uuid.uuid4().hex}"
    )

    result = _run_setup(app, env, "--yes")

    assert result.returncode != 0
    assert "over 200 characters" in result.stdout
    assert not (app / "data").exists()


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_no_answer_or_closed_stdin_declines_install(tmp_path: Path, answer: str) -> None:
    app = _copy_setup(tmp_path)
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = (
        rf"HKCU\Software\SpiceMakerSetupTests\{uuid.uuid4().hex}"
    )

    result = subprocess.run(
        [str(app / "Setup.cmd")],
        input=answer,
        text=True,
        capture_output=True,
        timeout=10,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "Install Python now? [y/N]" in result.stdout
    assert "Python 3.14 is required" in result.stdout
    assert not (app / "data").exists()


def test_wrong_hash_never_runs_download(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    fake_exe = tmp_path / "not-an-installer.exe"
    fake_exe.write_bytes(b"This is not a Python installer. Do not execute it.\n")
    pins = tmp_path / "test-pins.txt"
    arch = _host_pin_arch()
    pins.write_text(
        f"{arch}|{fake_exe.as_uri()}|{'0' * 64}|{fake_exe.stat().st_size}|0.0\n",
        encoding="ascii",
    )
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = (
        rf"HKCU\Software\SpiceMakerSetupTests\{uuid.uuid4().hex}"
    )
    env["SPICE_MAKER_TEST_PINS"] = str(pins)

    result = _run_setup(app, env, "--yes")

    assert result.returncode != 0
    assert "failed its SHA-256 check" in result.stdout
    assert not (app / "data" / "temp" / f"python-3.14.7-{arch}.exe").exists()


def test_remove_saved_shortcut_without_python(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    (app / "tools" / "python-install-pins.txt").unlink()
    shortcut_dir = tmp_path / "Desktop café"
    shortcut_dir.mkdir()
    shortcut = shortcut_dir / "Spice Maker.lnk"
    shortcut.write_text("fake shortcut", encoding="ascii")
    state = app / "data" / "shortcut.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"path": str(shortcut)}), encoding="utf-8")

    result = _run_setup(app, os.environ.copy(), "--remove")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not shortcut.exists()
    assert not state.exists()


def test_remove_shortcut_dir_override_without_python(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    (app / "tools" / "python-install-pins.txt").unlink()
    shortcut_dir = tmp_path / "custom Desktop"
    shortcut_dir.mkdir()
    shortcut = shortcut_dir / "Spice Maker.lnk"
    shortcut.write_text("fake shortcut", encoding="ascii")

    result = _run_setup(app, os.environ.copy(), "--remove", "--shortcut-dir", str(shortcut_dir))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not shortcut.exists()


def test_remove_refuses_other_shortcut_name(tmp_path: Path) -> None:
    app = _copy_setup(tmp_path)
    other = tmp_path / "Other.lnk"
    other.write_text("leave me alone", encoding="ascii")
    state = app / "data" / "shortcut.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"path": str(other)}), encoding="utf-8")

    result = _run_setup(app, os.environ.copy(), "--remove")

    assert result.returncode != 0
    assert "Unexpected shortcut path" in result.stdout
    assert other.exists()
    assert state.exists()


def test_registry_python_reaches_bootstrap(tmp_path: Path) -> None:
    import winreg

    _require_native_python()
    app = _copy_setup(tmp_path)
    (app / "tools" / "bootstrap.py").write_text(
        'import sys; print("STUB_BOOTSTRAP", sys.argv[1:])\n', encoding="ascii"
    )
    reg_root, reg_base = _test_registry_root()
    reg_install = rf"{reg_base}\3.14\InstallPath"
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, reg_install, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    ) as key:
        winreg.SetValueEx(key, "ExecutablePath", 0, winreg.REG_SZ, sys.executable)
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = reg_root
    try:
        result = _run_setup(app, env, "--yes", "--no-install-python")
    finally:
        _delete_test_registry(reg_base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STUB_BOOTSTRAP ['--yes', '--no-install-python']" in result.stdout


def test_non_python_registry_executable_is_rejected(tmp_path: Path) -> None:
    import winreg

    app = _copy_setup(tmp_path)
    fake_python = tmp_path / "store-alias.exe"
    _compile_stub(fake_python, "public class Stub { public static int Main() { return 0; } }")
    reg_root, reg_base = _test_registry_root()
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        rf"{reg_base}\3.14\InstallPath",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
    ) as key:
        winreg.SetValueEx(key, "ExecutablePath", 0, winreg.REG_SZ, str(fake_python))
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = reg_root
    try:
        result = _run_setup(app, env, "--yes", "--no-install-python")
    finally:
        _delete_test_registry(reg_base)

    assert result.returncode != 0
    assert "Python 3.14 is required" in result.stdout


def test_wrong_platform_python_is_rejected(tmp_path: Path) -> None:
    import winreg

    _require_native_python()
    app = _copy_setup(tmp_path)
    reg_root, reg_base = _test_registry_root()
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        rf"{reg_base}\3.14\InstallPath",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
    ) as key:
        winreg.SetValueEx(key, "ExecutablePath", 0, winreg.REG_SZ, sys.executable)
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = reg_root
    env.pop("PROCESSOR_ARCHITEW6432", None)
    env["PROCESSOR_ARCHITECTURE"] = "ARM64" if _host_pin_arch() == "amd64" else "AMD64"
    try:
        result = _run_setup(app, env, "--yes", "--no-install-python")
    finally:
        _delete_test_registry(reg_base)

    assert result.returncode != 0
    assert "Python 3.14 is required" in result.stdout


def test_yes_runs_hashed_stub_installer_and_redetects_python(tmp_path: Path) -> None:
    _require_native_python()

    app = _copy_setup(tmp_path)
    (app / "tools" / "bootstrap.py").write_text(
        'print("STUB_BOOTSTRAP_AFTER_INSTALL")\n', encoding="ascii"
    )
    stub = tmp_path / "stub-installer.exe"
    _compile_stub(
        stub,
        r"""
using System;
using System.IO;
using Microsoft.Win32;
public class StubInstall {
    public static int Main(string[] args) {
        var root = Environment.GetEnvironmentVariable("SPICE_MAKER_TEST_REGISTRY_ROOT");
        var python = Environment.GetEnvironmentVariable("SPICE_MAKER_TEST_STUB_PYTHON");
        var marker = Environment.GetEnvironmentVariable("SPICE_MAKER_TEST_STUB_MARKER");
        if (root == null || !root.StartsWith("HKCU\\") || python == null) return 2;
        using (var hive = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry64))
        using (var key = hive.CreateSubKey(root.Substring(5) + "\\3.14\\InstallPath")) {
            key.SetValue("ExecutablePath", python, RegistryValueKind.String);
        }
        File.WriteAllText(marker, "installer ran");
        return 0;
    }
}
""",
    )
    arch = _host_pin_arch()
    pins = tmp_path / "stub-pins.txt"
    pins.write_text(
        f"{arch}|{stub.as_uri()}|{hashlib.sha256(stub.read_bytes()).hexdigest()}|"
        f"{stub.stat().st_size}|0.1\n",
        encoding="ascii",
    )
    marker = tmp_path / "installer-ran.txt"
    reg_root, reg_base = _test_registry_root()
    env = os.environ.copy()
    env["SPICE_MAKER_TEST_REGISTRY_ROOT"] = reg_root
    env["SPICE_MAKER_TEST_PINS"] = str(pins)
    env["SPICE_MAKER_TEST_STUB_PYTHON"] = sys.executable
    env["SPICE_MAKER_TEST_STUB_MARKER"] = str(marker)
    try:
        result = _run_setup(app, env, "--yes")
    finally:
        _delete_test_registry(reg_base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="ascii") == "installer ran"
    assert "SHA-256 verified" in result.stdout
    assert "STUB_BOOTSTRAP_AFTER_INSTALL" in result.stdout
    assert not (app / "data" / "temp" / f"python-3.14.7-{arch}.exe").exists()
