"""Release installation instructions must check published bytes before launch."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "spice-maker-bob"
PACK = "SpiceMakerBob"
START = "<!-- install-steps:start -->"
END = "<!-- install-steps:end -->"
FORBIDDEN = (
    "set-executionpolicy",
    "-executionpolicy",
    "bypass",
    "unrestricted",
    "invoke-expression",
    "| iex",
    "iex ",
    "set-mppreference",
    "add-mppreference",
    "--insecure",
    "http://",
    "more info",
    "run anyway",
    "unblock",
)


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _section() -> str:
    text = _readme()
    assert text.count(START) == text.count(END) == 1
    return text.split(START, 1)[1].split(END, 1)[0]


def _published() -> str:
    text = (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8-sig")
    match = re.fullmatch(r"([0-9a-f]{64})  Install\.exe\s*", text)
    assert match, "SHA256SUMS.txt must hold one lowercase SHA-256 for Install.exe"
    return match.group(1)


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def _commands() -> list[str]:
    blocks = re.findall(r"```powershell\n(.*?)\n[ ]*```", _section(), flags=re.S)
    assert len(blocks) == 1, "the checked download and launch must be one script block"
    return [line.strip() for line in blocks[0].splitlines() if line.strip()]


def test_release_download_extract_check_and_launch_are_one_fail_fast_block() -> None:
    version = _version()
    assert _commands() == [
        "& {",
        "$ErrorActionPreference = 'Stop'",
        f"$spiceFolder = Join-Path $env:USERPROFILE 'Downloads\\{PACK}-{version}'",
        "if (Test-Path -LiteralPath $spiceFolder) { throw 'This version folder already exists; use its Start.cmd or choose a fresh folder name.' }",
        "New-Item -ItemType Directory -Path $spiceFolder | Out-Null",
        f"$spiceZip = Join-Path $spiceFolder '{PACK}-{version}-Windows-x64.zip'",
        "curl.exe --fail --location --proto =https --proto-redir =https -o $spiceZip "
        f"https://github.com/BasamAhmed640/{REPO}/releases/download/v{version}/{PACK}-{version}-Windows-x64.zip",
        "if ($LASTEXITCODE -ne 0) { throw 'Download failed; setup was not started.' }",
        "Expand-Archive -LiteralPath $spiceZip -DestinationPath $spiceFolder",
        "$spiceInstaller = Join-Path $spiceFolder 'Install.exe'",
        "if ((Get-FileHash -LiteralPath $spiceInstaller -Algorithm SHA256).Hash -ne "
        f"'{_published()}') {{ throw 'Installer checksum mismatch; setup was not started.' }}",
        "& $spiceInstaller",
        "}",
    ]


def test_every_checksum_in_the_steps_is_the_published_one() -> None:
    assert "RELEASE_INSTALLER_SHA256_PENDING" not in _section(), (
        "insert built installer checksum before release"
    )
    assert set(re.findall(r"\b[0-9a-fA-F]{64}\b", _section())) == {_published()}


def test_published_checksum_is_the_tracked_installer() -> None:
    installer = ROOT / "Install.exe"
    if not installer.is_file():
        pytest.skip("Install.exe is not in this checkout")
    with installer.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    assert digest == _published()


def test_installer_source_and_readme_versions_match() -> None:
    first = (ROOT / "INSTALL.txt").read_text(encoding="utf-8-sig").splitlines()[0]
    assert _version() in first
    assert f"Installer version **{_version()}**" in _section()


def test_install_links_are_https_and_this_repository() -> None:
    urls = re.findall(r"https?://[^\s`)>'\"]+", _section())
    assert urls
    home = f"https://github.com/BasamAhmed640/{REPO}"
    assert all(url == home or url.startswith(home + "/") for url in urls)
    assert "/archive/refs/heads/main.zip" not in _section()


def test_instructions_do_not_change_or_promise_to_avoid_os_protection() -> None:
    lowered = _section().lower()
    assert not any(pattern in lowered for pattern in FORBIDDEN)
    assert "may trigger a windows security prompt" in lowered
    assert "does not appear" not in lowered


def test_checksum_mismatch_really_prevents_next_statement(tmp_path: Path) -> None:
    shell = shutil.which("powershell.exe")
    if shell is None:
        pytest.skip("PowerShell is required for the Windows script control")
    candidate = tmp_path / "Install.exe"
    candidate.write_bytes(b"TEST_FIXTURE: intentionally not the release installer")
    commands = _commands()
    guard = next(line for line in commands if "Get-FileHash" in line)
    # Preserve the exact published guard. Replace only its expected checksum so
    # this test remains a mismatch control even when release hashes change.
    guard = re.sub(r"-ne '[^']+'", "-ne '" + "0" * 64 + "'", guard)
    literal = str(candidate).replace("'", "''")
    script = "& { $ErrorActionPreference = 'Stop'; $spiceInstaller = '" + literal
    script += "'; " + guard + "; 'REACHED_INSTALLER' }"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        # Do not make Windows PowerShell load a PowerShell 7 module path inherited
        # from the test runner; let this child choose its own built-in modules.
        env={key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert "Installer checksum mismatch" in result.stderr
    assert "REACHED_INSTALLER" not in result.stdout


def test_no_other_line_instructs_unchecked_installer_launch() -> None:
    outside = _readme().replace(_section(), "")
    assert re.findall(r"(?i)(?:double-click|run)\W{0,4}\**`?Install\.exe(?!\s+--)", outside) == []


def test_current_overview_keeps_evidence_and_archive_links() -> None:
    text = _readme()
    for link in (
        "docs/ENGINE_GUIDE.md",
        "docs/engine-refactor.html",
        "docs/archive/README-before-1.8.0.md",
    ):
        assert link in text
        assert (ROOT / link).is_file()
    assert "not mean most PCB components are implemented today" in text
    assert "RGP/QFN and an unspecified package are blocked" in text
    assert "MCU, FPGA, CPLD, processor, SoC" in text
