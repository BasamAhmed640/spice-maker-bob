"""`_capability_gate` names the offending project file instead of raising bare.

The gate reads user-supplied files (``models/capabilities/*.json`` and
``evidence/capability_map.json``). A malformed file must be a ``ValueError``
that names the path, because the CLI turns it into ``error: <path>: ...`` and
exit code 2 rather than a raw traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler.cli import _capability_gate


class _Project:
    """The only attribute ``_capability_gate`` reads from a project."""

    def __init__(self, root: Path) -> None:
        self.root = root


def _project(tmp_path: Path) -> _Project:
    (tmp_path / "models" / "capabilities").mkdir(parents=True)
    (tmp_path / "evidence").mkdir()
    return _Project(tmp_path)


def test_malformed_capability_file_names_the_path(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bad = tmp_path / "models" / "capabilities" / "U1.json"
    bad.write_text("{not json", encoding="utf-8")
    (tmp_path / "evidence" / "capability_map.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"U1\.json"):
        _capability_gate(project)


def test_malformed_capability_map_names_the_path(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (tmp_path / "evidence" / "capability_map.json").write_text("{nope", encoding="utf-8")

    with pytest.raises(ValueError, match=r"capability_map\.json"):
        _capability_gate(project)


def test_non_object_capability_map_is_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (tmp_path / "evidence" / "capability_map.json").write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match="expected a JSON object"):
        _capability_gate(project)
