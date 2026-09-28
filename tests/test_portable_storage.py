"""Regression checks for per-copy state, containment, and first-launch setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from boardmodeler import storage
from boardmodeler.config import AppConfig, config_path, load_config, save_config
from boardmodeler.security import credentials

_original_credential_path = credentials.credential_path


@pytest.fixture
def portable_root(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "credential_path", _original_credential_path)
    root = tmp_path / "extracted"
    root.mkdir()
    monkeypatch.setenv("SPICE_MAKER_ROOT", str(root))
    return root


def test_profile_and_config_override_never_restore_old_settings(
    portable_root, tmp_path, monkeypatch
):
    old = tmp_path / "profile" / "BoardModeler" / "config.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"default_model_dir": "OLD-LOCATION"}))
    monkeypatch.setenv("APPDATA", str(old.parent.parent))
    monkeypatch.setenv("LOCALAPPDATA", str(old.parent.parent))
    monkeypatch.setenv("BOARDMODELER_CONFIG", str(old))
    assert config_path() == portable_root / "data/config.json"
    assert load_config().default_model_dir is None
    assert not load_config().setup_complete
    assert credentials.credential_path() == portable_root / "data/credentials.bob.json"


def test_fresh_copy_and_relative_paths_do_not_share_state(portable_root, tmp_path, monkeypatch):
    save_config(AppConfig(default_model_dir="models/chips", setup_complete=True))
    assert storage.model_dir(load_config().default_model_dir) == portable_root / "models/chips"
    second = tmp_path / "another-download"
    monkeypatch.setenv("SPICE_MAKER_ROOT", str(second))
    assert not load_config().setup_complete
    assert storage.model_dir() == second / "models"
    second.mkdir()
    (portable_root / "data").rename(second / "data")
    assert storage.model_dir(load_config().default_model_dir) == second / "models/chips"


def test_outside_write_paths_and_link_escape_are_rejected(portable_root, tmp_path):
    with pytest.raises(ValueError, match="inside"):
        storage.local_path("../outside")
    with pytest.raises(ValueError):
        save_config(AppConfig(), tmp_path / "outside.json")
    assert not (tmp_path / "outside.json").exists()
    with pytest.raises(ValueError, match="inside"):
        storage.state_file("../../outside.json")
    with pytest.raises(ValueError, match="inside"):
        storage.state_file(str(tmp_path / "outside.json"))
    assert storage.state_file("config.json") == portable_root / "data/config.json"


def test_temporary_files_and_bob_profile_stay_inside_copy(portable_root, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    for key in ("TEMP", "TMP", "TMPDIR", "MPLCONFIGDIR"):
        monkeypatch.setenv(key, "unused")
    storage.initialize()
    with tempfile.TemporaryDirectory() as folder:
        assert Path(folder).is_relative_to(portable_root)
    env = storage.bob_environment({"PATH": "existing-tools"})
    assert env["PATH"] == "existing-tools"
    for key in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TEMP",
        "TMP",
    ):
        assert Path(env[key]).is_relative_to(portable_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows encryption")
def test_saved_key_stays_in_this_copy_and_does_not_cross_copies(
    portable_root, tmp_path, monkeypatch
):
    secret = "test-only-portable-secret"
    credentials.set_credential("fixture", secret)
    path = credentials.credential_path()
    assert path == portable_root / "data/credentials.bob.json"
    assert credentials.get_credential("fixture").value == secret
    monkeypatch.setenv("SPICE_MAKER_ROOT", str(tmp_path / "fresh-copy"))
    assert credentials.credential_path() != path
    assert credentials.get_credential("fixture").value is None


def test_launcher_root_keeps_state_inside_the_extracted_folder(portable_root, tmp_path):
    """The launcher root keeps config and credentials inside its own folder."""
    outside = tmp_path / "outside-config.json"
    code = """
import sys
from pathlib import Path
from boardmodeler.config import config_path
from boardmodeler.security.credentials import credential_path
from boardmodeler.storage import app_root, data_dir
root = app_root()
assert root == Path(sys.argv[1]), root
for path in (data_dir(), config_path(), credential_path()):
    assert path.is_relative_to(root), path
print('contained')
"""
    environment = {k: v for k, v in os.environ.items() if k != "SPICE_MAKER_ROOT"}
    environment["SPICE_MAKER_ROOT"] = str(portable_root)
    environment["BOARDMODELER_CONFIG"] = str(outside)
    result = subprocess.run(
        [sys.executable, "-c", code, str(portable_root)],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "contained" in result.stdout
    assert not outside.exists()


def test_unfrozen_write_guard_in_subprocess(portable_root, tmp_path):
    """Containment follows the process, not the build: ``SPICE_MAKER_ROOT`` suffices.

    The folder-local ``.venv`` runs the command line with ``SPICE_MAKER_ROOT`` set and
    ``sys.frozen`` is absent, and the write guard still installs. A separate process
    avoids installing a permanent audit hook in the
    test runner.
    """
    code = """
import sys
from pathlib import Path
from boardmodeler.storage import initialize, install_write_guard, portable
assert not getattr(sys, 'frozen', False), 'this case is the unfrozen venv runtime'
assert portable(), 'SPICE_MAKER_ROOT makes this process contained'
initialize()
install_write_guard()
(Path(sys.argv[1]) / 'data' / 'inside.txt').write_text('local')
try:
    Path(sys.argv[2]).write_text('must fail')
except PermissionError:
    print('blocked')
else:
    raise AssertionError('write outside the root succeeded')
"""
    environment = {k: v for k, v in os.environ.items() if k != "SPICE_MAKER_ROOT"}
    environment["SPICE_MAKER_ROOT"] = str(portable_root)
    result = subprocess.run(
        [sys.executable, "-c", code, str(portable_root), str(tmp_path / "outside.txt")],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "blocked" in result.stdout
    assert (portable_root / "data/inside.txt").read_text() == "local"
    assert not (tmp_path / "outside.txt").exists()


def test_contained_cli_entry_point_installs_the_write_guard(portable_root, tmp_path):
    """The venv runtime's real entry point (``python -m boardmodeler.cli``) installs it.

    ``install_write_guard()`` only helps if a contained process actually calls it, so
    this enters through ``boardmodeler.cli.main`` the way ``Boardmodeler.cmd`` does.
    """
    code = """
import sys
from pathlib import Path
from boardmodeler.cli import main
assert main(['version']) == 0
(Path(sys.argv[1]) / 'data' / 'inside.txt').write_text('local')
try:
    Path(sys.argv[2]).write_text('must fail')
except PermissionError:
    print('blocked')
else:
    raise AssertionError('write outside the root succeeded')
"""
    environment = {k: v for k, v in os.environ.items() if k != "SPICE_MAKER_ROOT"}
    environment["SPICE_MAKER_ROOT"] = str(portable_root)
    result = subprocess.run(
        [sys.executable, "-c", code, str(portable_root), str(tmp_path / "outside.txt")],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "blocked" in result.stdout
    assert (portable_root / "data/inside.txt").read_text() == "local"
    assert not (tmp_path / "outside.txt").exists()


def test_write_guard_installs_nothing_in_a_repo_dev_run(tmp_path):
    """Without ``SPICE_MAKER_ROOT``, an ordinary developer run stays unguarded."""
    code = """
import sys
from pathlib import Path
from boardmodeler.storage import install_write_guard, portable
assert not portable(), 'this case is a repo dev run'
install_write_guard()
Path(sys.argv[1]).write_text('dev run')
print('installed nothing')
"""
    environment = {k: v for k, v in os.environ.items() if k != "SPICE_MAKER_ROOT"}
    target = tmp_path / "dev-write.txt"
    result = subprocess.run(
        [sys.executable, "-c", code, str(target)],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "installed nothing" in result.stdout
    assert target.read_text() == "dev run"


def test_setup_requires_explicit_ltspice_and_local_model_folder(
    portable_root, tmp_path, monkeypatch
):
    from boardmodeler import agent_providers, setup_wizard

    with pytest.raises(setup_wizard.SetupError, match="has not been selected"):
        setup_wizard._check_ltspice(None)
    assert not config_path().exists()
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"test fixture")
    monkeypatch.setattr(setup_wizard, "locate", lambda path: SimpleNamespace(path=path))
    monkeypatch.setattr(
        setup_wizard,
        "smoke_test",
        lambda *args: SimpleNamespace(status="pass", detail=""),
    )
    assert setup_wizard._check_ltspice(str(fake_exe)) == str(fake_exe)
    with pytest.raises(ValueError, match="inside"):
        setup_wizard._model_folder(str(tmp_path / "outside"))
    assert not config_path().exists()
    monkeypatch.setenv("SPICE_TEST_KEY", "test-only-key")
    monkeypatch.setattr("boardmodeler.cli.doctor_payload", lambda: {"ok": True})
    monkeypatch.setattr("boardmodeler.cli._render_doctor_human", lambda report: "Doctor OK")
    args = SimpleNamespace(
        ltspice=str(fake_exe),
        model_dir=str(portable_root / "models"),
        provider=agent_providers.default_provider().id,
        internet="off",
        key_env="SPICE_TEST_KEY",
        yes=True,
    )
    assert setup_wizard._save(args) == 0
    assert load_config().setup_complete
    assert load_config().default_model_dir == "models"
