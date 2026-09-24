"""Startup never looks for LTspice; an explicitly configured one runs a real ``.op`` deck.

Both tests run ``tools/fresh_zip_check.py``'s own ``--internal`` modes in a child
interpreter whose data/config location is a fresh temporary folder, so the probe the
release check trusts is exactly the probe exercised here.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "fresh_zip_check.py"


def _tool():
    spec = importlib.util.spec_from_file_location("fresh_zip_check", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_env(root: Path) -> dict[str, str]:
    env = _tool().child_environment(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_internal(root: Path, *mode: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--internal", *mode],
        cwd=root,
        env=_clean_env(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    result = _tool().parse_result(completed.stdout)
    assert result is not None, completed.stdout[-2000:] + completed.stderr[-2000:]
    return result


def test_startup_doctor_settings_and_ui_import_never_touch_ltspice(tmp_path_factory) -> None:
    root = tmp_path_factory.mktemp("clean-root")
    assert "ltspice" not in str(root).lower()
    result = _run_internal(root, "startup-probe")

    assert result["events"] == []
    assert result["doctor_exit"] == 0
    assert result["doctor_ltspice"] == {
        "found": False,
        "path": None,
        "reason": "unset",
        "setup_required": True,
        "searched": False,
        "probed": [],
    }
    assert result["config_ltspice_path"] is None
    assert result["settings_ltspice_path"] is None
    assert result["locate_reason"] == "unset"
    assert {"app", "model_maker", "setup_dialog"} <= set(result["ui_imported"])
    assert result["ui_import_errors"] == {}
    assert result["qt_application_created"] is False
    assert _tool().probe_verdict(result)[0]


def test_the_probe_is_not_blind(tmp_path) -> None:
    """Positive control: every kind of search the startup test forbids is recorded."""
    fake_root = tmp_path / "Programs" / "ADI"
    fake_root.mkdir(parents=True)
    script = (
        "import glob, os, subprocess, sys\n"
        f"sys.path.insert(0, {str(TOOL.parent)!r})\n"
        "import fresh_zip_check as tool\n"
        "rec = tool._Recorder(())\n"
        "rec.wrap_stat_probes()\n"
        "sys.addaudithook(rec.audit)\n"
        r"os.path.isfile(r'C:\Program Files\ADI\LTspice\LTspice.exe')"
        "\n"
        "os.listdir(os.path.join(os.environ['LOCALAPPDATA'], 'Programs', 'ADI'))\n"
        r"glob.glob(r'C:\Program Files\LTC\*\XVIIx64.exe')"
        "\n"
        "try:\n"
        "    subprocess.Popen(['LTspice.exe', '-version'])\n"
        "except OSError:\n"
        "    pass\n"
        "tool._emit({'events': rec.events})\n"
    )
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    result = _tool().parse_result(completed.stdout)
    assert result is not None, completed.stderr[-2000:]
    seen = {event["event"] for event in result["events"]}
    assert {"py:isfile", "os.listdir", "glob.glob", "subprocess.Popen"} <= seen, seen


@pytest.mark.ltspice
def test_explicitly_configured_ltspice_runs_an_op_deck(tmp_path_factory, request) -> None:
    supplied = os.environ.get("LTSPICE_EXE")
    exe = Path(supplied) if supplied else None
    if exe is None or not exe.is_file():
        install = request.getfixturevalue("session_ltspice_install")
        exe = install.path if install is not None else None
    if exe is None or not exe.is_file():
        pytest.skip("no LTspice executable configured; set LTSPICE_EXE=<LTspice.exe> to run")
    root = tmp_path_factory.mktemp("configured-root")
    result = _run_internal(root, "configured-run", "--internal-arg", str(exe))

    assert result["reason"] == "config", result
    assert Path(result["resolved"]) == exe
    assert Path(result["config_file"]).is_relative_to(root)
    assert result["status"] == "PASS", result
    assert abs(result["v_out"] - 2.5) <= 1e-3
