"""Keep the extraction, authoring and verification core byte-identical in both editions.

Spice Maker and Spice Maker Bob differ only in their provider, window, CLI and
packaging layers. The modules listed in ``shared_core.json`` are the common core:
datasheet reading and extraction, the probe harness, the convergence checks and the
verification engine. Each edition commits the same manifest (path -> sha256), so a
change to a core file in one repository fails ``tests/test_shared_core.py`` until the
manifest is regenerated — and a regenerated manifest that differs from the sibling's
shows the drift in review.

    python tools/shared_core.py --check                      # this repo matches its manifest
    python tools/shared_core.py --compare ..\\spice-maker-bob  # both repos share the same bytes
    python tools/shared_core.py --write                      # after an intended core change
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("src") / "boardmodeler"
MANIFEST = ROOT / "shared_core.json"
CORE_DIRS = ("authoring", "documents", "requirements", "simulation", "verification")
#: Edition-specific modules inside the core directories (provider wiring and loop glue).
EDITION_SPECIFIC = {
    "authoring/api_backend.py",
    "authoring/backends.py",
    "authoring/deck_policy.py",
    "authoring/loop.py",
    "authoring/sanity.py",
    "documents/ocr.py",
    "simulation/ltspice.py",
}


def core_files(root: Path = ROOT) -> list[str]:
    package = root / PACKAGE
    return sorted(
        path.relative_to(package).as_posix()
        for folder in CORE_DIRS
        for path in (package / folder).glob("*.py")
        if path.relative_to(package).as_posix() not in EDITION_SPECIFIC
    )


def digest(root: Path, relative: str) -> str:
    return hashlib.sha256((root / PACKAGE / relative).read_bytes()).hexdigest()


def current(root: Path = ROOT) -> dict[str, str]:
    return {relative: digest(root, relative) for relative in core_files(root)}


def check(root: Path = ROOT) -> list[str]:
    """Differences between the committed manifest and the files on disk."""
    expected = json.loads((root / "shared_core.json").read_text(encoding="utf-8"))
    actual = current(root)
    problems = [f"missing core file: {name}" for name in sorted(set(expected) - set(actual))]
    problems += [f"unlisted core file: {name}" for name in sorted(set(actual) - set(expected))]
    problems += [
        f"changed core file: {name}"
        for name in sorted(set(expected) & set(actual))
        if expected[name] != actual[name]
    ]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    group.add_argument("--compare", type=Path, metavar="OTHER_REPO")
    args = parser.parse_args(argv)
    if args.write:
        MANIFEST.write_text(json.dumps(current(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST.name}: {len(current())} core files")
        return 0
    if args.check:
        problems = check()
        print("\n".join(problems) or f"shared core intact: {len(current())} files")
        return 1 if problems else 0
    other = args.compare.resolve()
    mine, theirs = current(), current(other)
    drift = sorted(
        name for name in set(mine) | set(theirs) if mine.get(name) != theirs.get(name)
    )
    print("\n".join(f"differs: {name}" for name in drift) or f"identical: {len(mine)} files")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
