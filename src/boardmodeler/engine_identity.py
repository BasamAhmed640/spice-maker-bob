"""Identify the engine shipped in a wheel or frozen desktop application.

A version number alone cannot distinguish two builds of the same release.
The frozen snapshot is made from the same source used to build the wheel.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DEFAULT_ENGINE = "behavioral"
ENGINES = ("behavioral", "pin_only", "legacy_ai")
SNAPSHOT_NAME = "engine-identity.json"


def source_contract(package: Path) -> dict:
    """Hash executable policy, independent of checkout line endings and timestamps."""
    files = sorted(
        path
        for path in package.rglob("*")
        if path.is_file() and path.suffix in (".py", ".json") and path.name != SNAPSHOT_NAME
    )
    if not files:
        raise ValueError("engine_identity_source_missing")
    entries = [
        (
            path.relative_to(package).as_posix(),
            hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        )
        for path in files
    ]
    digest = hashlib.sha256(
        json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "default_engine": DEFAULT_ENGINE,
        "engines": list(ENGINES),
        "source_sha256": digest,
    }


def engine_contract() -> dict:
    """Report actual installed source or the snapshot embedded at freeze time."""
    package = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        snapshot = json.loads((package / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("default_engine") != DEFAULT_ENGINE
            or snapshot.get("engines") != list(ENGINES)
            or len(str(snapshot.get("source_sha256", ""))) != 64
        ):
            raise ValueError("engine_identity_snapshot_invalid")
        return snapshot
    return source_contract(package)
