"""The README's Windows install steps: this repository only, HTTPS only, checked before run.

The steps let someone start ``Install.exe`` without the SmartScreen box by downloading it
without the internet mark, so the checksum check is what stands between them and a wrong
file. These tests pin the exact commands and fail when the published checksum stops matching
``SHA256SUMS.txt`` or the tracked installer, so a rebuild cannot leave a stale check behind.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "spice-maker-bob"
START = "<!-- install-steps:start -->"
END = "<!-- install-steps:end -->"
BACKSLASH = "\\"

#: Anything that would switch off a Windows protection or run downloaded text as code.
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
)


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _section() -> str:
    text = _readme()
    assert text.count(START) == 1, "exactly one marked install section"
    assert text.count(END) == 1, "exactly one marked install section"
    return text.split(START, 1)[1].split(END, 1)[0]


def _published() -> str:
    text = (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8-sig")
    match = re.fullmatch(r"([0-9a-f]{64})  Install\.exe\s*", text)
    assert match, "SHA256SUMS.txt must hold one lowercase SHA-256 for Install.exe"
    return match.group(1)


def _commands() -> list[str]:
    blocks = re.findall(r"```powershell\n(.*?)\n[ ]*```", _section(), flags=re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def test_the_commands_are_exactly_the_reviewed_ones() -> None:
    installer = "." + BACKSLASH + f"{REPO}-main" + BACKSLASH + "Install.exe"
    assert _commands() == [
        "cd ~" + BACKSLASH + "Downloads",
        f"curl.exe --fail --location --proto =https -o {REPO}.zip "
        f"https://github.com/BasamAhmed640/{REPO}/archive/refs/heads/main.zip",
        f"tar -xf {REPO}.zip",
        f"if ((Get-FileHash {installer} -Algorithm SHA256).Hash -eq '{_published()}') "
        "{ 'OK: Install.exe matches the published checksum' } "
        "else { 'STOP: Install.exe does not match the published checksum' }",
        installer,
    ]


def test_every_checksum_in_the_steps_is_the_published_one() -> None:
    assert set(re.findall(r"\b[0-9a-fA-F]{64}\b", _section())) == {_published()}


def test_the_published_checksum_is_the_tracked_installer() -> None:
    installer = ROOT / "Install.exe"
    if not installer.is_file():
        pytest.skip("Install.exe is not in this checkout")
    digest = hashlib.sha256()
    with installer.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    assert digest.hexdigest() == _published()


def test_the_steps_name_the_installer_version() -> None:
    first = (ROOT / "INSTALL.txt").read_text(encoding="utf-8-sig").splitlines()[0]
    version = re.search(r"\d+\.\d+\.\d+", first)
    assert version, first
    assert f"**{version.group(0)}**" in _section()


def test_every_link_is_https_and_this_repository() -> None:
    urls = re.findall(r"https?://[^\s`)>'\"]+", _section())
    assert urls
    home = f"https://github.com/BasamAhmed640/{REPO}"
    for url in urls:
        assert url == home or url.startswith(home + "/"), url


def test_nothing_switches_off_a_protection_or_runs_downloaded_text() -> None:
    lowered = _section().lower()
    for pattern in FORBIDDEN:
        assert pattern not in lowered, pattern


def test_no_other_line_says_to_run_the_installer_unchecked() -> None:
    # A command line with flags (``Install.exe --silent``) describes the automated release
    # check, not a step for a person, so it is not an unchecked instruction.
    outside = _readme().replace(_section(), "")
    pattern = r"(?i)(?:double-click|run)\W{0,4}(?:its included\W+)?\**`?Install\.exe(?!\s+--)"
    assert re.findall(pattern, outside) == []
