"""Unsupported required supply connections stop pin-only builds before simulation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.pipeline import test_support_gate as helpers

from boardmodeler.authoring import viability


def test_required_secondary_supply_blocks_without_simulation_or_agent_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Synthetic pin table reproducing the structural issue: distinct required
    # supply terminals with no extracted domain assignment.
    pins = [
        helpers._pin("VIN", "power", 1, requirement="required"),
        helpers._pin("GND", "ground", 2, requirement="required"),
        helpers._pin("VCC", "power", 3, requirement="required"),
        helpers._pin("HB", "power", 4, requirement="required"),
    ]
    monkeypatch.setattr(
        viability, "run_gate", lambda *args, **kwargs: pytest.fail("must refuse before LTspice")
    )
    result, backend = helpers._run(
        tmp_path, monkeypatch, helpers.AMPLIFIER_WORDING, pins=pins, engine="pin_only"
    )

    assert result.status == "BLOCKED"
    assert result.detail.startswith("pin_only_required_secondary_supply:")
    assert backend.turns == 0
    assert (result.lib_path, result.asy_path, result.card_path) == (None, None, None)
    assert not (tmp_path / "out" / "build" / "pin-only-gate").exists()
    timing = json.loads((tmp_path / "out" / "run-timing.json").read_text(encoding="utf-8"))
    assert timing["route"] == "pin_only_refused"
    assert timing["provider_calls"] == 0 and timing["provider_calls_complete"] is True
