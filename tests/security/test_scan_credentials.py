"""``tools/scan_credentials.py`` finds planted keys and never prints them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "scan_credentials.py"
FAKE_NAME = "SCANNER_TEST_API_KEY"
FAKE_VALUE = "planted" + "-fake-value-" + "7f3a9c1e5b2d"


def _scan(root: Path, *extra: str, env_value: str = FAKE_VALUE):
    env = dict(os.environ, **{FAKE_NAME: env_value})
    return subprocess.run(
        [sys.executable, str(TOOL), str(root), *extra],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_planted_environment_value_and_token_shape_are_found(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text(f"one\ntwo\nkey={FAKE_VALUE}\n", encoding="utf-8")
    token = "gh" + "p_" + "A1b2C3d4" * 5
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "ci.yml").write_text(f"token: {token}\n", encoding="utf-8")

    completed = _scan(tmp_path, "--json")
    assert completed.returncode == 1
    assert FAKE_VALUE not in completed.stdout + completed.stderr
    assert token not in completed.stdout + completed.stderr
    findings = json.loads(completed.stdout)["findings"]
    assert {"path": "notes.txt", "line": 3, "kind": "env_value", "name": FAKE_NAME} in findings
    assert {"path": "sub/ci.yml", "line": 1, "kind": "pattern", "name": "github_token"} in findings

    human = _scan(tmp_path)
    assert human.returncode == 1
    assert f"notes.txt:3: value of ${FAKE_NAME}" in human.stdout
    assert FAKE_VALUE not in human.stdout + human.stderr


def test_skipped_locations_short_values_and_clean_trees_exit_zero(tmp_path: Path) -> None:
    for relative in (".venv/a.txt", ".git/b.txt", "__pycache__/c.txt", "node_modules/d.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAKE_VALUE, encoding="utf-8")
    (tmp_path / "datasheet.pdf").write_text(FAKE_VALUE, encoding="utf-8")
    (tmp_path / "wave.raw").write_text(FAKE_VALUE, encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" + FAKE_VALUE.encode())
    (tmp_path / "short.txt").write_text("value=shortkey1\n", encoding="utf-8")

    completed = _scan(tmp_path, "--json")
    assert completed.returncode == 0, completed.stdout
    assert json.loads(completed.stdout)["findings"] == []
    short = _scan(tmp_path, "--json", env_value="shortkey1")
    assert short.returncode == 0 and FAKE_NAME not in json.loads(short.stdout)["env_names_checked"]
