"""LTspice remains invisible until this copy's saved path is explicitly set."""

from __future__ import annotations

import pytest

from boardmodeler.config import AppConfig, LtspiceConfig, save_config
from boardmodeler.simulation import ltspice


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.cli.config_path", lambda: target)
    return target


def test_unconfigured_path_never_uses_environment_or_install_locations(tmp_path, monkeypatch):
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"fixture")
    monkeypatch.setenv("LTSPICE_EXE", str(fake_exe))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    outcome = ltspice.locate_outcome()
    assert outcome.reason == "unset"
    assert outcome.install is None
    assert outcome.probed == []
    assert ltspice.discover().reason == "unset"
    assert ltspice.default_lib_dir() is None


def test_saved_path_is_only_source(isolated_config, tmp_path):
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"fixture")
    save_config(AppConfig(ltspice=LtspiceConfig(path=str(fake_exe))), isolated_config)
    outcome = ltspice.locate_outcome()
    assert outcome.reason == "config"
    assert outcome.install is not None and outcome.install.path == fake_exe
    assert outcome.probed == [(fake_exe, "config")]


def test_missing_saved_path_does_not_fall_back(isolated_config, tmp_path, monkeypatch):
    missing = tmp_path / "missing.exe"
    other = tmp_path / "LTspice.exe"
    other.write_bytes(b"fixture")
    monkeypatch.setenv("LTSPICE_EXE", str(other))
    save_config(AppConfig(ltspice=LtspiceConfig(path=str(missing))), isolated_config)
    outcome = ltspice.locate_outcome()
    assert outcome.reason == "config_missing"
    assert outcome.install is None
    assert outcome.probed == [(missing, "config")]


def test_doctor_reports_setup_required_without_search(isolated_config):
    from boardmodeler.cli import doctor_payload

    payload = doctor_payload(run_smoke=False)
    section = payload["ltspice"]
    assert section["found"] is False and section["path"] is None
    assert section["reason"] == "unset"
    assert section["setup_required"] is True
    assert section["probed"] == [] and section["searched"] is False
    assert "SETUP" in section["smoke_detail"]
