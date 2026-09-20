"""Shared pytest fixtures.

``ltspice_exe`` skips (never fakes) when the simulator is unavailable; tests that
assert simulator behaviour must be able to prove they ran against a real
executable, so the fixture also exposes the located path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler.simulation.ltspice import LtspiceInstall, locate


@pytest.fixture(scope="session")
def ltspice_install() -> LtspiceInstall:
    install = locate()
    if install is None:
        pytest.skip("LTspice was not found on this machine")
    if not install.path.is_file():
        pytest.skip(f"LTspice candidate {install.path} does not exist")
    return install


@pytest.fixture(scope="session")
def ltspice_exe(ltspice_install: LtspiceInstall) -> Path:
    return ltspice_install.path


@pytest.fixture(autouse=True)
def isolated_credential_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "boardmodeler.security.credentials.credential_path", lambda: tmp_path / "credentials.bin"
    )
