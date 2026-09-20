"""CLI tests (Phase 0 step 11).

`doctor --json` is the environment contract used by the GUI and by CI, so its
keys are asserted, and a forced-bad `LTSPICE_EXE` must produce
``smoke_test == "fail"`` with a non-empty detail rather than a crash or a false
pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from boardmodeler.cli import doctor_payload, main

pytestmark = pytest.mark.ltspice

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(
    args: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "boardmodeler.cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
        timeout=300,
        check=False,
    )


def test_version_command() -> None:
    assert main(["version"]) == 0
    assert main(["version", "--json"]) == 0


def test_doctor_json_keys_and_real_smoke_pass(ltspice_exe: Path, tmp_path: Path) -> None:
    proc = _run_cli(["doctor", "--json"], env={"BOARDMODELER_CONFIG": str(tmp_path / "cfg.json")})
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)

    assert payload["tool"] == "boardmodeler"
    assert payload["telemetry"] == "none"
    for key in ("version", "python", "platform", "config", "ltspice", "reader_backend", "ocr"):
        assert key in payload, key

    ltspice = payload["ltspice"]
    assert ltspice["found"] is True, ltspice
    assert Path(ltspice["path"]).is_file()
    assert ltspice["version"] == "26.0.0"
    assert ltspice["smoke_test"] == "pass", ltspice.get("smoke_detail")
    assert ltspice["smoke_detail"].strip()
    assert ltspice["exit_code"] == 0
    assert ltspice["measured_v"] == pytest.approx(0.632, rel=0.02)
    assert ltspice["raw_sha256"] and len(ltspice["raw_sha256"]) == 64
    assert ltspice["log_sha256"] and len(ltspice["log_sha256"]) == 64
    assert ltspice["smoke_workdir"]

    backend = payload["reader_backend"]
    assert backend["reader_backend"] in ("native", "spicelib")
    assert backend["agreement_tolerance"] == 1e-9
    assert backend["detail"].strip()
    assert payload["ok"] is True


def test_doctor_reports_failure_for_bad_ltspice_env(tmp_path: Path) -> None:
    bogus = tmp_path / "definitely-not-ltspice.exe"
    proc = _run_cli(
        ["doctor", "--json"],
        env={
            "LTSPICE_EXE": str(bogus),
            "BOARDMODELER_CONFIG": str(tmp_path / "cfg.json"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    ltspice = payload["ltspice"]
    # A configured-but-absent executable must be reported, not silently replaced
    # by the installed one.
    assert ltspice["found"] is False, ltspice
    assert ltspice["smoke_test"] == "fail"
    assert ltspice["smoke_detail"].strip()
    assert str(bogus) in ltspice["smoke_detail"]
    assert payload["ok"] is False


def test_doctor_no_smoke_never_reports_pass(tmp_path: Path) -> None:
    payload = doctor_payload(run_smoke=False)
    assert payload["ltspice"]["smoke_test"] is None
    assert "skipped" in payload["ltspice"]["smoke_detail"]


def test_doctor_human_output_is_printable(ltspice_exe: Path, tmp_path: Path) -> None:
    proc = _run_cli(
        ["doctor", "--no-smoke"], env={"BOARDMODELER_CONFIG": str(tmp_path / "cfg.json")}
    )
    assert proc.returncode == 0, proc.stderr
    assert "boardmodeler" in proc.stdout
    assert "ltspice:" in proc.stdout
    assert "reader backend:" in proc.stdout
    assert "telemetry: none" in proc.stdout


def test_unknown_command_exits_nonzero() -> None:
    proc = _run_cli(["not-a-command"])
    assert proc.returncode != 0


def test_doctor_credentials_report_the_source_the_backend_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor variable the reason advertises must show up as a source, not 'missing'."""
    from boardmodeler import cli
    from boardmodeler.agent_providers import CATALOG, ids
    from boardmodeler.authoring import api_backend
    from boardmodeler.security import credentials

    class EmptyKeyring:
        def get_password(self, service: str, key: str) -> None:
            return None

    monkeypatch.setattr(credentials, "_read_saved", lambda: None)
    for provider in CATALOG:
        for variable in api_backend.env_sources(provider):
            monkeypatch.delenv(variable, raising=False)

    # The section lists this build's providers and the source each key resolves from:
    # one advertised variable is exported, so its provider must read 'env' and every
    # other provider this build accepts must read 'missing'.
    exported = next((p for p in CATALOG if p.env_aliases), None)
    if exported is not None:
        monkeypatch.setenv(api_backend.env_sources(exported)[1], "sk-doctor-secret")

    section = cli._credentials_section()

    assert set(section) == set(ids())
    for provider in CATALOG:
        source = "env" if provider.id == getattr(exported, "id", None) else "missing"
        assert f"source={source}" in section[provider.id], section[provider.id]
    assert "sk-doctor-secret" not in json.dumps(section)
