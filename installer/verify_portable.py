"""Exercise the shipped installer: local extraction, relaunch, update and fresh copy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from verify_gui import verify


def run(repo: Path) -> dict:
    work = repo / "build" / ("portable-check-" + uuid.uuid4().hex[:8])
    first, second = work / "copy-one", work / "copy-two"
    for folder in (first, second):
        folder.mkdir(parents=True)
        shutil.copy2(repo / "Install.exe", folder / "Install.exe")
        subprocess.run(
            [str(folder / "Install.exe"), "--silent", "--no-launch"], check=True, timeout=60
        )
    exe = first / "app/SpiceMaker.exe"
    assert (
        hashlib.sha256(exe.read_bytes()).digest()
        == hashlib.sha256((repo / "dist/SpiceMaker/SpiceMaker.exe").read_bytes()).digest()
    )
    assert not (first / "data/config.json").exists()
    startup = verify(exe, repo / "build/portable-first-launch.png")
    assert "setup" in startup["title"].lower(), startup
    data = first / "data"
    data.mkdir(exist_ok=True)
    config = {"setup_complete": True, "default_model_dir": "models", "web_reinforcement": False}
    (data / "config.json").write_text(json.dumps(config))
    # Test-owned ciphertext sentinel checks installer byte preservation; never a real key.
    (data / "credentials.bin").write_bytes(b"test-owned-encrypted-file-sentinel")
    before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
    subprocess.run([str(first / "Install.exe"), "--silent", "--no-launch"], check=True, timeout=60)
    assert all((data / name).read_bytes() == value for name, value in before.items())
    assert not (second / "data/config.json").exists()
    assert not (second / "data/credentials.bin").exists()
    main = verify(exe, repo / "build/portable-main-window.png")
    assert "1.3.0" in main["title"]
    env = os.environ.copy()
    env["BOARDMODELER_CONFIG"] = str(data / "config.json")
    check = subprocess.run(
        [str(second / "app/SpiceMaker.exe"), "--cli", "setup", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    payload = json.loads(check.stdout)
    assert Path(payload["config_path"]).is_relative_to(second)
    assert payload["model_dir"] is None
    report = {
        "status": "PASS",
        "first_launch": startup["title"],
        "relaunch": main["title"],
        "same_folder_update_preserves_data": True,
        "fresh_copy_has_no_saved_settings_or_key": True,
        "ignores_external_config": True,
        "root": str(first),
        "installer_sha256": hashlib.sha256((repo / "Install.exe").read_bytes()).hexdigest(),
        "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }
    (repo / "build/portable-verification.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    print(json.dumps(run(Path(__file__).resolve().parents[1]), indent=2))
