"""Shared pytest fixtures.

The application deliberately never searches for LTspice: the path is a setting the
user makes explicitly. A *test session* therefore says which executable it means,
once, via the documented ``LTSPICE_EXE`` automation override. ``session_ltspice_env``
resolves the install the same way the ``ltspice_install`` fixture does (configured
first, then one explicit ``discover``) and exports the path into the real process
environment, so in-process ``locate()`` calls and the CLI subprocesses a test spawns
both see an explicitly configured simulator.

``ltspice_exe`` skips (never fakes) when the simulator is unavailable; tests that
assert simulator behaviour must be able to prove they ran against a real
executable, so the fixture also exposes the located path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from boardmodeler.simulation.ltspice import LtspiceInstall, discover, locate

#: How a test session states its simulator choice. The application only ever reads
#: this as a *configured* path; setting it is the explicit ask, not a search.
_CONFIGURED_ENV = "LTSPICE_EXE"


def _resolve_install() -> LtspiceInstall | None:
    """The session's simulator: already configured, else found by one explicit ask."""
    install = locate() or discover().install
    if install is None or not install.path.is_file():
        return None
    return install


def _no_simulator_message(install: LtspiceInstall | None) -> str:
    """Say what is true: nothing was configured/found, and how to configure one."""
    if install is None:
        return (
            "no LTspice executable was configured or found, and the test session did not "
            f"configure one; set {_CONFIGURED_ENV} to an LTspice.exe to run this test"
        )
    return (
        f"the LTspice candidate {install.path} does not exist, and the test session did "
        f"not configure one; set {_CONFIGURED_ENV} to an LTspice.exe to run this test"
    )


@pytest.fixture(scope="session")
def session_ltspice_install() -> LtspiceInstall | None:
    """Resolve the session's simulator once; ``None`` is a result, not a skip."""
    return _resolve_install()


@pytest.fixture(scope="session", autouse=True)
def session_ltspice_env(session_ltspice_install: LtspiceInstall | None) -> Iterator[None]:
    """Export the session's simulator choice so subprocesses inherit it.

    The environment is mutated for real (through a session-scoped ``MonkeyPatch``)
    rather than with the function-scoped ``monkeypatch`` fixture: a session fixture
    cannot request that fixture, and a CLI started with ``subprocess.run`` inherits
    only ``os.environ``, not pytest's patching.
    """
    if session_ltspice_install is None:
        yield
        return
    patch = pytest.MonkeyPatch()
    patch.setenv(_CONFIGURED_ENV, str(session_ltspice_install.path))
    try:
        yield
    finally:
        patch.undo()


@pytest.fixture(scope="session")
def ltspice_install(session_ltspice_install: LtspiceInstall | None) -> LtspiceInstall:
    if session_ltspice_install is None:
        pytest.skip(_no_simulator_message(None))
    if not session_ltspice_install.path.is_file():
        pytest.skip(_no_simulator_message(session_ltspice_install))
    return session_ltspice_install


@pytest.fixture(scope="session")
def ltspice_exe(ltspice_install: LtspiceInstall) -> Path:
    return ltspice_install.path


@pytest.fixture(autouse=True)
def isolated_credential_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "boardmodeler.security.credentials.credential_path",
        lambda: tmp_path / "credentials.json",
    )
