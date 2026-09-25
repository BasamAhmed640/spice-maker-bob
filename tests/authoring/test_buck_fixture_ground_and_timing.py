"""The two defects a saved TPS54332DDA GUI run exposed, as fixture-level checks.

The saved current-limit bench (B002_REQ_016) measured 1.21198e-9 A against a 4.2 A
minimum: the planner's GND had been renamed to a node nothing tied to node 0, and the
0.02 ohm sense resistor between PH and the inductor hid the power stage from the
soft-start rule, so a 0.5 ms window was accepted while the cited SS charging needs
3.86 ms to reach the minimum reference.
"""

from __future__ import annotations

import pytest

from boardmodeler.authoring.circuit_probe import CircuitRecipe
from boardmodeler.authoring.test_planner import _buck_fixture_issue, _series_sense_nodes

#: The saved run's own cited rows (statements and limits as extracted).
SOURCE_ROWS = (
    ("Slow-start charge current is 2 μA typical at V(SS) = 0.4 V.", {"typ": 2.0, "unit": "μA"}),
    (
        "Voltage reference is 0.772 V minimum, 0.8 V typical and 0.828 V maximum.",
        {"min": 0.772, "typ": 0.8, "max": 0.828, "unit": "V"},
    ),
)
STATEMENT = "Current limit threshold is 4.2 A minimum and 6.5 A maximum at VIN = 12 V."


def _recipe(*, ground: str = "gnd", start: float = 5e-4, stop: float = 1e-3) -> dict:
    """The saved B002_REQ_016 bench, with the ground spelled as the planner wrote it."""
    g = ground
    return {
        "purpose": "Measure switch current limit threshold",
        "unit": "A",
        "terminals": {
            "BOOT": "boot",
            "COMP": "comp",
            "EN": "en",
            "GND": g,
            "PH": "ph",
            "POWERPAD": g,
            "SS": "ss",
            "VIN": "vin",
            "VSENSE": "vsense",
        },
        "components": [
            f"V1 vin {g} 12",
            f"V2 en {g} 3.3",
            "Cboot boot ph 0.1u",
            f"Dcatch {g} ph BM_CATCH",
            "Rsense ph sw 0.02",
            "L1 sw vout 10u",
            f"C1 vout {g} 22u",
            f"Rload vout {g} 0.5",
            "R1 vout vsense 31.25k",
            f"R2 vsense {g} 10k",
            f"C2 ss {g} 10n",
            "Rcomp comp comp_c 10k",
            f"Ccomp comp_c {g} 1n",
        ],
        "stop": stop,
        "step": 5e-8,
        "measurement": {
            "operation": "max",
            "signal": "V(ph,sw)",
            "start": start,
            "end": stop,
            "scale": 50.0,
        },
        "condition_evidence": "Current limit 4.2-6.5 A at VIN = 12 V; 0.02 ohm sense to PH.",
    }


def test_a_bench_grounded_only_through_gnd_keeps_gnd_as_node_0():
    recipe = CircuitRecipe.model_validate(_recipe())
    assert recipe.terminals["GND"] == "gnd"
    assert not any("bm_fixture_ground" in line for line in recipe.components)


def test_a_ground_current_sense_bench_still_keeps_its_distinct_node():
    data = _recipe()
    data["components"] = [*data["components"], "Vgsense gnd 0 0"]
    recipe = CircuitRecipe.model_validate(data)
    assert recipe.terminals["GND"] == "bm_fixture_ground"


def test_the_saved_floating_bench_is_refused_rather_than_simulated():
    with pytest.raises(ValueError, match="fixture_floating_ground"):
        CircuitRecipe.model_validate(_recipe(ground="bm_fixture_ground"))


def test_the_sense_resistor_no_longer_hides_the_inductor():
    lines = [line.split() for line in _recipe()["components"]]
    assert _series_sense_nodes(lines, "ph", "gnd") == {"ph", "sw"}


def test_the_saved_current_limit_window_is_rejected_as_premature():
    recipe = CircuitRecipe.model_validate(_recipe())
    issue = _buck_fixture_issue(recipe, SOURCE_ROWS, STATEMENT)
    assert issue is not None and issue.startswith("buck_fixture_soft_start")
    assert "0.00386" in issue  # 10 nF x 0.772 V / 2 uA


def test_a_window_after_cited_soft_start_is_accepted():
    recipe = CircuitRecipe.model_validate(_recipe(start=5e-3, stop=8e-3))
    assert _buck_fixture_issue(recipe, SOURCE_ROWS, STATEMENT) is None
