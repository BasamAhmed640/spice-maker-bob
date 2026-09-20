"""Copy shared source/tests/packaging between explicit checkouts, retaining flavor.

Run with --check in CI/development to detect drift; --apply updates tracked shared
files without deleting destination files. Repository history and docs remain local.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    target = args.target.resolve()
    if source == target or not (target / ".git").exists():
        parser.error("target must be a different git checkout")
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=source, text=True
    ).splitlines()
    changed = []
    for name in paths:
        if name in ("src/boardmodeler/build_flavor.py", "installer/assets/pepper-splash.gif"):
            continue
        if not name.startswith(("src/", "tests/", "installer/", ".github/", "tools/")):
            continue
        src, dst = source / name, target / name
        if not dst.resolve().is_relative_to(target):
            raise ValueError("destination resolves outside checkout")
        if src.is_file() and (not dst.is_file() or src.read_bytes() != dst.read_bytes()):
            changed.append(name)
            if args.apply:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
    print(f"{len(changed)} shared files {'updated' if args.apply else 'differ'}")
    for name in changed:
        print(name)
    return 0 if args.apply or not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
