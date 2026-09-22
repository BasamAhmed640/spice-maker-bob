"""LTspice is resolved from a saved setting, never by searching the machine.

The owner's rule: SETUP must be completed explicitly, and startup must not snoop for
an installation. ``locate``/``locate_outcome`` therefore read only the configured
path or ``LTSPICE_EXE``; the well-known install locations are probed by
:func:`boardmodeler.simulation.ltspice.discover` and only when the user asks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler.config import AppConfig, LtspiceConfig, save_config
from boardmodeler.simulation import ltspice


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No ambient simulator and a throwaway config file for every test here."""
    target = tmp_path / "config.json"
    monkeypatch.delenv("SPICE_MAKER_ROOT", raising=False)
    monkeypatch.delenv("LTSPICE_EXE", raising=False)
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.cli.config_path", lambda: target)
    return target


@pytest.fixture(autouse=True)
def no_install_locations(monkeypatch, tmp_path):
    """Keep discovery hermetic: no real Program Files / LocalAppData is consulted."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local"))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "empty-program-files"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "empty-program-files-x86"))


def _poison_install_probes(monkeypatch) -> None:
    """Fail loudly if any code stats a well-known install location."""
    candidates = {path for path, _source in ltspice._install_candidates()}
    assert candidates, "this test needs candidate locations to guard"
    real_is_file = Path.is_file

    def guarded(self):
        assert self not in candidates, f"an install location was probed: {self}"
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", guarded)


def test_locate_is_unset_and_probes_nothing(isolated_config, monkeypatch):
    _poison_install_probes(monkeypatch)
    assert not isolated_config.exists()
    assert ltspice.locate() is None
    outcome = ltspice.locate_outcome()
    assert outcome.reason == "unset"
    assert outcome.install is None
    assert outcome.probed == []


def test_locate_uses_the_saved_config_path(isolated_config, tmp_path):
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"test fixture")
    save_config(AppConfig(ltspice=LtspiceConfig(path=str(fake_exe))), isolated_config)
    outcome = ltspice.locate_outcome()
    assert outcome.reason == "config" and outcome.install is not None
    assert outcome.install.path == fake_exe
    assert outcome.install.source == "config"
    assert outcome.probed == [(fake_exe, "config")]
    assert ltspice.locate() == outcome.install


def test_a_missing_configured_path_is_reported_not_replaced(isolated_config, tmp_path, monkeypatch):
    missing = tmp_path / "not-there.exe"
    save_config(AppConfig(ltspice=LtspiceConfig(path=str(missing))), isolated_config)
    real_exe = tmp_path / "real-LTspice.exe"
    real_exe.write_bytes(b"test fixture")
    monkeypatch.setenv("LTSPICE_EXE", str(real_exe))
    outcome = ltspice.locate_outcome()
    assert outcome.install is None and outcome.reason == "config_missing"
    assert outcome.probed == [(missing, "config")], "the env override must not replace a setting"


def test_the_environment_override_resolves_when_nothing_is_configured(tmp_path, monkeypatch):
    real_exe = tmp_path / "real-LTspice.exe"
    real_exe.write_bytes(b"test fixture")
    monkeypatch.setenv("LTSPICE_EXE", str(real_exe))
    outcome = ltspice.locate_outcome()
    assert outcome.install is not None and outcome.install.source == "env:LTSPICE_EXE"
    assert outcome.reason == "env"
    monkeypatch.setenv("LTSPICE_EXE", str(tmp_path / "missing.exe"))
    assert ltspice.locate_outcome().reason == "env_missing"


def test_discover_probes_install_locations_only_when_asked(tmp_path, monkeypatch):
    installed = tmp_path / "local" / "Programs" / "ADI" / "LTspice" / "LTspice.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"test fixture")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    # Nothing was asked for, so nothing is probed and nothing is resolved.
    with pytest.MonkeyPatch.context() as patch:
        _poison_install_probes(patch)
        assert ltspice.locate() is None

    outcome = ltspice.discover()
    assert outcome.install is not None and outcome.install.path == installed
    assert outcome.install.source == "LOCALAPPDATA"
    assert outcome.reason == "discovered"
    assert (installed, "LOCALAPPDATA") in outcome.probed


def test_discover_reports_not_installed_with_the_probed_list(tmp_path):
    outcome = ltspice.discover()
    assert outcome.install is None and outcome.reason == "not_installed"
    assert outcome.probed_paths, "an explicit search must say where it looked"


def test_doctor_reports_setup_required_and_probes_nothing(isolated_config, monkeypatch):
    from boardmodeler.cli import doctor_payload

    _poison_install_probes(monkeypatch)
    payload = doctor_payload(run_smoke=False)
    section = payload["ltspice"]
    assert section["found"] is False and section["path"] is None
    assert section["reason"] == "unset"
    assert section["setup_required"] is True
    assert section["probed"] == []
    assert "SETUP" in section["smoke_detail"]
    assert section["smoke_test"] is None
    assert payload["ok"] is False


def test_doctor_searches_install_locations_only_with_the_explicit_flag(tmp_path, monkeypatch):
    from boardmodeler.cli import doctor_payload

    installed = tmp_path / "local" / "Programs" / "ADI" / "LTspice" / "LTspice.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"test fixture")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    quiet = doctor_payload(run_smoke=False)
    assert quiet["ltspice"]["found"] is False and quiet["ltspice"]["searched"] is False

    found = doctor_payload(run_smoke=False, find_ltspice=True)
    section = found["ltspice"]
    assert section["found"] is True and Path(section["path"]) == installed
    assert section["source"] == "LOCALAPPDATA"
    assert section["reason"] == "discovered"
    assert section["searched"] is True
