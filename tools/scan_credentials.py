"""Scan a tree for credentials: live environment secrets and high-confidence key shapes.

    python tools/scan_credentials.py [ROOT] [--json]

(a) The *values* of this process's environment variables whose names end in ``_API_KEY``,
    ``_TOKEN`` or ``_SECRET`` (values shorter than 12 characters are ignored) are looked
    for in every text file. They are compared in memory only.
(b) Generic high-confidence secret patterns (``sk-...``, ``ghp_...``, quoted
    ``"api_key": "..."`` with a long value, private-key blocks, ...).

Output is the file path, line number and the variable or pattern NAME -- never a value
or the matching text. Skipped: ``.venv``, ``.git``, ``__pycache__``, ``node_modules``,
``*.pdf``, ``*.raw`` and binary files. Exit 1 on findings, 0 when clean.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = frozenset({".venv", ".git", "__pycache__", "node_modules"})
SKIP_SUFFIXES = frozenset(
    {
        ".pdf",
        ".raw",
        ".exe",
        ".dll",
        ".pyd",
        ".so",
        ".dylib",
        ".zip",
        ".7z",
        ".gz",
        ".bz2",
        ".xz",
        ".tar",
        ".whl",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".icns",
        ".pyc",
        ".pyo",
        ".db",
        ".sqlite",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".msi",
        ".cab",
        ".npz",
        ".npy",
        ".pkl",
        ".qm",
        ".lnk",
    }
)
SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
MIN_VALUE_LENGTH = 12
MAX_FILE_BYTES = 20 * 1024 * 1024

PATTERNS: dict[str, re.Pattern[str]] = {
    "sk_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "sk_prefixed_key": re.compile(r"\bsk-(?:proj|ant|or|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\bgh[opsur]_[A-Za-z0-9]{30,}"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"),
    "quoted_api_key": re.compile(r"""(?i)["']api_?key["']\s*[:=]\s*["'][^"'\s]{20,}["']"""),
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----"),
}


def secret_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Name -> value for the variables whose values are searched for (kept in memory)."""
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name.upper().endswith(SECRET_NAME_SUFFIXES) and len(value) >= MIN_VALUE_LENGTH
    }


def iter_files(root: Path):
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix.lower() in SKIP_SUFFIXES or path.is_symlink():
                continue
            yield path


def read_text(path: Path) -> str | None:
    """The file as text, or ``None`` for binaries, oversize or unreadable files."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan(root: Path, environ: dict[str, str] | None = None) -> dict[str, object]:
    secrets = secret_environment(environ)
    findings: list[dict[str, object]] = []
    scanned = skipped = 0
    for path in iter_files(root):
        text = read_text(path)
        if text is None:
            skipped += 1
            continue
        scanned += 1
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for name, value in secrets.items():
                if value in line:
                    findings.append(
                        {"path": relative, "line": number, "kind": "env_value", "name": name}
                    )
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        {"path": relative, "line": number, "kind": "pattern", "name": name}
                    )
    return {
        "root": str(root),
        "files_scanned": scanned,
        "files_skipped": skipped,
        "env_names_checked": sorted(secrets),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "root",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1]),
        help="directory to scan (default: this repository)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    report = scan(root)
    findings = report["findings"]
    assert isinstance(findings, list)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in findings:
            what = "value of $" if item["kind"] == "env_value" else "pattern "
            print(f"{item['path']}:{item['line']}: {what}{item['name']}")
        print(
            f"{len(findings)} finding(s); {report['files_scanned']} files scanned, "
            f"{report['files_skipped']} skipped; env names checked: "
            f"{', '.join(report['env_names_checked']) or 'none'}",
            file=sys.stderr if findings else sys.stdout,
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
