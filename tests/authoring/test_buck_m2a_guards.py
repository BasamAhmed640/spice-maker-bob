"""Pre-freeze buck guard controls; cited numbers are synthetic test records."""

from __future__ import annotations

import pytest

from boardmodeler.authoring.buck_fixtures import current_limit_recipe
from boardmodeler.authoring.test_planner import (
    validate_buck_gain_sweep,
    validate_partial_plan,
    validate_plan,
)
from boardmodeler.domain.records import Limit
from boardmodeler.models.buck_switching import match_pins
from tests.requirements.test_model import make_requirement


def _row(req_id: str, statement: str, limits: Limit | None, *, page: int = 5):
    return make_requirement(
        req_id=req_id,
        statement=statement,
        excerpt=statement,
        limits=limits,
        page=page,
        section="TEST_FIXTURE cited buck rows",
    ).model_copy(update={"citation_verified": True})


GAIN = _row(
    "B002_TPS54332DDA_SW_CURRENT_TO_COMP",
    "Switch current to COMP transconductance: 12 A/V typical with VIN = 12 V.",
    Limit(typ=12, unit="A/V"),
)
LIMIT = _row(
    "B002_TPS54332DDA_ILIM",
    "Current limit threshold: 4.2 A min, 6.5 A max with VIN = 12 V.",
    Limit(min=4.2, max=6.5, unit="A"),
)
VECO = _row(
    "B003_REQ_TPS54332_ECOMODE_COMP_0P5V",
    "In Eco-mode, COMP is clamped at 0.5 V during pulse skipping.",
    Limit(typ=0.5, unit="V"),
    page=12,
)
SS_CHARGE = _row(
    "B002_TPS54332DDA_SS_CHARGE",
    "Slow-start (SS) pin charge current: 2 μA typical with V(SS) = 0.4 V.",
    Limit(typ=2e-6, unit="A"),
)
VREF = _row(
    "B002_TPS54332DDA_VREF",
    "Voltage reference: 0.772 V min, 0.8 V typical, 0.828 V max.",
    Limit(min=0.772, typ=0.8, max=0.828, unit="V"),
)
CATCH = _row(
    "B001_PIN_CATCH",
    "An external catch diode is required between PH and GND.",
    None,
    page=2,
)


def _recipe(*, aliases: bool = False):
    ports = (
        ("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND")
        if aliases
        else ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "POWERPAD")
    )
    match = match_pins(ports)
    assert match.ok, match.reason
    return current_limit_recipe(
        match.roles,
        match.ground_ties,
        vin=12,
        charge_current=2e-6,
        reference_min=0.772,
        reference_typ=0.8,
        reference_max=0.828,
        limit_high=6.5,
        evidence="TEST_FIXTURE copies of verified TPS54332DDA limits, PDF page index 5.",
    )


def _plan(
    requirement, recipe, *, sources=(VECO, SS_CHARGE, VREF, CATCH), partial=False, unverified=None
):
    payload = {
        "bindings": [
            {"req_id": requirement.req_id, "probe": "circuit_measurement", "recipe": recipe}
        ]
    }
    context = [row.model_dump(mode="json", by_alias=True) for row in sources]
    validate = validate_partial_plan if partial else validate_plan
    return validate(
        payload, [requirement], tuple(recipe["terminals"]), unverified or {}, context=context
    )


def test_gain_sweep_accepts_distinct_active_points_and_rejects_single_point():
    assert validate_buck_gain_sweep((0.55, 0.6, 0.65), [VECO]) == 0.5
    for points in ((0.55,), (0.55, 0.55)):
        with pytest.raises(ValueError, match="buck_fixture_gain_needs_two_points"):
            validate_buck_gain_sweep(points, [VECO])


@pytest.mark.parametrize("points", [(0.5, 0.55), (0.45, 0.6)])
def test_gain_sweep_rejects_comp_at_or_below_cited_veco(points):
    with pytest.raises(ValueError, match=r"buck_fixture_gain_comp_inactive.*0.5 V"):
        validate_buck_gain_sweep(points, [VECO])
    assert validate_buck_gain_sweep((0.55, 0.6), [VECO]) == 0.5


def test_gain_sweep_needs_a_verified_voltage_citation():
    with pytest.raises(ValueError, match="buck_fixture_gain_veco_unverified"):
        validate_buck_gain_sweep(
            (0.55, 0.6), [VECO.model_copy(update={"citation_verified": False})]
        )
    with pytest.raises(ValueError, match="buck_fixture_gain_veco_unverified"):
        validate_buck_gain_sweep((0.55, 0.6), [VECO], unverified={VECO.req_id})
    assert validate_buck_gain_sweep((0.55, 0.6), [VECO]) == 0.5


@pytest.mark.parametrize("aliases", [False, True])
def test_legacy_scalar_buck_gain_is_an_explicit_gap_but_valid_sweep_can_be_built(aliases):
    recipe = _recipe(aliases=aliases)
    recipe["unit"] = "A/V"
    rows = _plan(GAIN, recipe, partial=True)
    assert rows[0]["probe"] is None
    assert "buck_fixture_gain_needs_sweep" in rows[0]["not_testable_reason"]
    assert validate_buck_gain_sweep((0.55, 0.6), [VECO]) == 0.5


def test_legacy_buck_gain_without_veco_citation_stays_unknown():
    recipe = _recipe()
    recipe["unit"] = "A/V"
    rows = _plan(
        GAIN, recipe, sources=(VECO.model_copy(update={"citation_verified": False}),), partial=True
    )
    assert rows[0]["probe"] is None
    assert "buck_fixture_gain_veco_unverified" in rows[0]["not_testable_reason"]
    withheld = _plan(GAIN, recipe, sources=(VECO,), partial=True, unverified={VECO.req_id})
    assert withheld[0]["probe"] is None
    assert "buck_fixture_gain_veco_unverified" in withheld[0]["not_testable_reason"]
    assert GAIN.limits.typ == 12


@pytest.mark.parametrize(
    "source",
    ["Iload vout 0 7", "Iload 0 vout 7", "Bload vout 0 I=7", "Bload 0 vout I=7"],
)
def test_current_limit_rejects_ideal_output_current_sink_in_both_orientations(source):
    recipe = _recipe()
    recipe["components"].append(source)
    with pytest.raises(ValueError, match="buck_fixture_current_limit_ideal_sink"):
        _plan(LIMIT, recipe)
    assert _plan(LIMIT, _recipe())[0]["probe"] == "circuit_measurement"


def test_current_limit_output_sense_resistor_cannot_hide_ideal_sink():
    faulty = _recipe()
    faulty["components"].extend(["Rsense_out vout out 0.02", "Iload out 0 7"])
    with pytest.raises(ValueError, match="buck_fixture_current_limit_ideal_sink"):
        _plan(LIMIT, faulty)
    assert _plan(LIMIT, _recipe())[0]["probe"] == "circuit_measurement"


def test_current_limit_aliases_cannot_bypass_sink_guard():
    clean = _recipe(aliases=True)
    assert _plan(LIMIT, clean)[0]["probe"] == "circuit_measurement"
    faulty = _recipe(aliases=True)
    faulty["components"].append("Iload vout 0 7")
    with pytest.raises(ValueError, match="buck_fixture_current_limit_ideal_sink"):
        _plan(LIMIT, faulty)


@pytest.mark.parametrize("aliases", [False, True])
def test_current_limit_starts_after_highest_cited_reference_plus_settling(aliases):
    clean = _recipe(aliases=aliases)
    assert clean["measurement"]["start"] == pytest.approx(0.00514)
    assert _plan(LIMIT, clean)[0]["probe"] == "circuit_measurement"
    early = _recipe(aliases=aliases)
    early["measurement"]["start"] = 0.00513
    with pytest.raises(ValueError, match=r"buck_fixture_soft_start.*0\.00514 s"):
        _plan(LIMIT, early)


def test_positive_cited_min_charge_current_takes_precedence_over_typical():
    slow_charge = _row(
        SS_CHARGE.req_id,
        SS_CHARGE.statement,
        Limit(min=1e-6, typ=2e-6, unit="A"),
    )
    early = _recipe()
    with pytest.raises(ValueError, match=r"buck_fixture_soft_start.*0\.00928 s"):
        _plan(LIMIT, early, sources=(slow_charge, VREF, CATCH))
    clean = _recipe()
    clean["measurement"]["start"] = 0.00928
    clean["measurement"]["end"] = 0.01128
    clean["stop"] = 0.01128
    assert (
        _plan(LIMIT, clean, sources=(slow_charge, VREF, CATCH))[0]["probe"] == "circuit_measurement"
    )


def test_startup_observation_can_measure_charging_but_current_limit_cannot():
    startup = _row(
        "TEST_STARTUP_SHAPE",
        "Startup output voltage rises during soft-start.",
        Limit(typ=3.3, unit="V"),
    )
    observing = _recipe()
    observing["purpose"] = "TEST_FIXTURE startup ramp observation"
    observing["unit"] = "V"
    observing["stop"] = 0.002
    observing["measurement"] = {
        "operation": "max",
        "signal": "V(vout)",
        "start": 0.001,
        "end": 0.002,
    }
    assert _plan(startup, observing)[0]["probe"] == "circuit_measurement"
    premature_limit = _recipe()
    premature_limit["purpose"] = "TEST_FIXTURE startup current limit"
    premature_limit["measurement"]["start"] = 0.001
    with pytest.raises(ValueError, match="buck_fixture_soft_start"):
        _plan(LIMIT, premature_limit)


def test_current_limit_missing_cited_charge_current_is_an_explicit_gap():
    missing = _plan(LIMIT, _recipe(), sources=(VREF, CATCH), partial=True)
    assert missing[0]["probe"] is None
    assert "buck_fixture_soft_start_evidence_missing" in missing[0]["not_testable_reason"]
    assert _plan(LIMIT, _recipe())[0]["probe"] == "circuit_measurement"


def test_current_limit_missing_ss_cap_is_rejected_but_connected_cap_is_clean():
    faulty = _recipe()
    faulty["components"] = [line for line in faulty["components"] if not line.startswith("C2 ")]
    with pytest.raises(ValueError, match="buck_fixture_soft_start_cap_missing"):
        _plan(LIMIT, faulty)
    assert _plan(LIMIT, _recipe())[0]["probe"] == "circuit_measurement"


def test_unrelated_opamp_ac_gain_is_unchanged():
    req = _row("OPAMP_GAIN", "Op amp voltage gain is 2 V/V.", Limit(typ=2, unit="V/V"))
    recipe = {
        "purpose": "TEST_FIXTURE op amp AC gain",
        "unit": "V/V",
        "terminals": {"IN": "vin", "OUT": "vout", "GND": "0"},
        "components": ["Vdrive vin 0 AC 1", "Rload vout 0 1k"],
        "analysis": "ac",
        "frequency_start": 1,
        "frequency_end": 1000000,
        "measurement": {
            "operation": "gain",
            "signal": "V(vout)",
            "reference": "V(vin)",
            "start": 1,
            "end": 1000,
        },
        "condition_evidence": "TEST_FIXTURE synthetic AC gain condition.",
    }
    assert _plan(req, recipe, sources=())[0]["probe"] == "circuit_measurement"
