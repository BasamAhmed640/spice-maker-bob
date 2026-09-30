"""Same-version installers must still carry the current model engine."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def verify(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "installer"))
    return importlib.import_module("verify_portable")


@pytest.mark.parametrize("surface", ["installed_wheel", "desktop_executable"])
@pytest.mark.parametrize("stale", [None, "b" * 64])
def test_same_version_cannot_hide_missing_or_different_engine(verify, surface, stale):
    expected = {
        "schema_version": 1,
        "default_engine": "behavioral",
        "engines": ["behavioral", "pin_only", "legacy_ai"],
        "source_sha256": "a" * 64,
    }
    payload = {"version": "1.7.0"}
    if stale is not None:
        payload["engine_contract"] = {**expected, "source_sha256": stale}
    with pytest.raises(RuntimeError, match=r"packaged.*stale"):
        verify._validate_engine_identity(payload, expected, surface)
    payload["engine_contract"] = dict(expected)
    assert verify._validate_engine_identity(payload, expected, surface) == expected
