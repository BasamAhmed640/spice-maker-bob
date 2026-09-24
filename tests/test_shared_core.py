"""The extraction/authoring/verification core matches the manifest both editions commit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("shared_core", ROOT / "tools" / "shared_core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_files_match_the_committed_manifest() -> None:
    assert _tool().check(ROOT) == []


def test_the_convergence_and_extraction_core_is_part_of_the_shared_manifest() -> None:
    files = _tool().core_files(ROOT)
    for name in (
        "authoring/convergence.py",
        "authoring/harness.py",
        "documents/relevance.py",
        "requirements/extract.py",
        "verification/engine.py",
    ):
        assert name in files
