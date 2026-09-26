"""Pre-freeze checks for synthetic buck benches; these are not device data."""

from __future__ import annotations

import pytest

from boardmodeler.authoring.circuit_probe import CircuitRecipe, make_probe
from boardmodeler.authoring.test_planner import validate_partial_plan, validate_plan
from boardmodeler.domain.records import Limit
from tests.requirements.test_model import make_requirement


def _recipe():
    return {
        "purpose": "TEST_FIXTURE active buck output",
        "unit": "V",
        "terminals": {"VIN": "vin", "PH": "ph", "COMP": "comp", "VSENSE": "sense", "GND": "0"},
        "components": [
            "Vvin vin 0 12",
            "Cvin vin 0 10u",
            "Lout ph out 4.7u",
            "Cout out 0 47u",
            "Rload out 0 2.5",
            "Rupper out sense 10k",
            "Rlower sense 0 5k",
            "Rcomp comp 0 10k",
            "Ccomp comp 0 1n",
        ],
        "stop": 0.001,
        "step": 1e-6,
        "measurement": {"operation": "mean", "signal": "V(out)", "start": 0.0009, "end": 0.001},
        "condition_evidence": "TEST_FIXTURE: 12 V input, 2.5 ohm load; not device data.",
    }


def _context():
    return [
        {
            "statement": "An external catch diode is required between PH and GND.",
            "limits": None,
            "citation_verified": True,
        },
        {
            "statement": "Error amplifier source/sink current at COMP is ±7 uA.",
            "limits": Limit(typ=7e-6, unit="A").model_dump(),
            "citation_verified": True,
        },
        {
            "statement": "In Eco-mode, COMP is clamped at 0.5 V during pulse skipping.",
            "limits": Limit(typ=0.5, unit="V").model_dump(),
            "citation_verified": True,
        },
    ]


def _validate(data, *, context=None):
    req = make_requirement(section="Electrical Characteristics").model_copy(
        update={"citation_verified": True}
    )
    return validate_plan(
        {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": data}]},
        [req],
        tuple(data["terminals"]),
        {},
        context=_context() if context is None else context,
    )


def test_missing_or_reversed_catch_diode_is_rejected_before_freeze():
    data = _recipe()
    with pytest.raises(ValueError, match="buck_fixture_missing_catch_diode"):
        _validate(data)
    data["components"].append("Dcatch ph 0 BM_CATCH")
    with pytest.raises(ValueError, match="anode to ground and cathode to PH"):
        _validate(data)


def test_comp_shunt_cannot_hold_cited_pulse_skip_level():
    data = _recipe()
    data["components"].append("Dcatch 0 ph BM_CATCH")
    with pytest.raises(ValueError, match=r"buck_fixture_comp_shunt.*7e-06 A.*0.07 V"):
        _validate(data)
    data["components"][7] = "Rcomp comp 0 100k"
    accepted = _validate(data)
    assert accepted[0]["probe"] == "circuit_measurement"
    assert accepted[0]["recipe"]["measurement"]["end"] == 0.001


def test_correctly_oriented_legacy_behavioral_catch_path_is_accepted():
    data = _recipe()
    data["components"][7] = "Rcomp comp 0 100k"
    data["components"].append("Bdiode ph 0 I=if(V(ph)<0, V(ph)/0.1, 0)")
    assert _validate(data)[0]["probe"] == "circuit_measurement"
    data["components"][-1] = "Bdiode ph 0 I=if(V(ph)>0, V(ph)/0.1, 0)"
    with pytest.raises(ValueError, match="buck_fixture_missing_catch_diode"):
        _validate(data)


def test_active_buck_fixture_needs_an_output_capacitor():
    data = _recipe()
    data["components"] = [line for line in data["components"] if not line.startswith("Cout ")]
    data["components"].append("Dcatch 0 ph BM_CATCH")
    with pytest.raises(ValueError, match="buck_fixture_missing_output_capacitor"):
        _validate(data)


def test_steady_state_window_cannot_precede_cited_soft_start_charging():
    data = _recipe()
    data["components"][7] = "Rcomp comp 0 100k"
    data["components"].extend(["Dcatch 0 ph BM_CATCH", "Css ss 0 10n"])
    data["terminals"]["SS"] = "ss"
    context = [
        *_context(),
        {
            "statement": "Slow-start (SS) pin charge current: 2 uA typical.",
            "limits": Limit(typ=2e-6, unit="A").model_dump(),
            "citation_verified": True,
        },
        {
            "statement": "Voltage reference: 0.772 V min, 0.8 V typical.",
            "limits": Limit(min=0.772, typ=0.8, unit="V").model_dump(),
            "citation_verified": True,
        },
    ]
    with pytest.raises(ValueError, match=r"buck_fixture_soft_start.*0\.005 s"):
        _validate(data, context=context)
    data["stop"] = 0.006
    data["measurement"].update(start=0.005, end=0.006)
    assert _validate(data, context=context)[0]["probe"] == "circuit_measurement"


def test_invalid_fixture_becomes_an_explicit_gap_without_changing_the_limit():
    data = _recipe()
    req = make_requirement(section="Electrical Characteristics").model_copy(
        update={"citation_verified": True}
    )
    rows = validate_partial_plan(
        {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": data}]},
        [req],
        tuple(data["terminals"]),
        {},
        context=_context(),
    )
    assert rows[0]["probe"] is None
    assert "buck_fixture_missing_catch_diode" in rows[0]["not_testable_reason"]
    assert req.limits.typ == 3.3


def test_diode_model_is_fixed_and_inserted_by_the_renderer(tmp_path):
    data = _recipe()
    data["components"].append("Dcatch 0 ph OTHER_MODEL")
    with pytest.raises(ValueError, match="fixed BM_CATCH"):
        CircuitRecipe.model_validate(data)
    data["components"][-1] = "Dcatch 0 ph BM_CATCH"
    parsed = CircuitRecipe.model_validate(data)
    model = tmp_path / "dut.lib"
    model.write_text(".subckt DUT VIN PH COMP VSENSE GND\n.ends DUT\n")
    deck = make_probe(parsed.model_dump()).render(model_lib=model, subckt="DUT", params={})
    assert "Dcatch 0 ph BM_CATCH" in deck
    assert deck.count(".model BM_CATCH D(") == 1


@pytest.mark.ltspice
def test_fixed_catch_diode_deck_runs_in_ltspice(tmp_path, ltspice_exe):
    from boardmodeler.simulation.ltspice import run_batch

    data = _recipe()
    data["components"][7] = "Rcomp comp 0 100k"
    data["components"].append("Dcatch 0 ph BM_CATCH")
    model = tmp_path / "dut.lib"
    model.write_text(
        ".subckt DUT VIN PH COMP VSENSE GND\nRpass VIN PH 10\nRbias COMP GND 1Meg\n.ends DUT\n"
    )
    probe = make_probe(data)
    deck = tmp_path / "deck.cir"
    deck.write_text(probe.render(model_lib=model, subckt="DUT", params={}))
    observed = run_batch(ltspice_exe, deck, tmp_path, timeout_s=30)
    assert observed.raw_path and observed.raw_path.is_file(), observed.observed()
    assert probe.measure(observed.raw_path, {})["recipe_value"] == pytest.approx(2.4, rel=0.03)


def test_inactive_shutdown_fixture_does_not_need_a_catch_diode():
    data = _recipe()
    data["components"].append("Ven en 0 0")
    data["terminals"]["EN"] = "en"
    req = make_requirement(section="Electrical Characteristics").model_copy(
        update={"citation_verified": True}
    )
    rows = validate_plan(
        {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": data}]},
        [req],
        tuple(data["terminals"]),
        {},
        context=_context(),
    )
    assert rows[0]["probe"] == "circuit_measurement"


def test_cited_current_direction_cannot_be_erased_by_absolute_measurement():
    req = make_requirement(
        statement="Current flows into the input pin at 1 uA typical.",
        excerpt="Current flows into the input pin at 1 uA typical.",
        limits=Limit(typ=1e-6, unit="A"),
        section="Electrical Characteristics",
    ).model_copy(update={"citation_verified": True})
    data = {
        "purpose": "TEST_FIXTURE input current",
        "unit": "A",
        "terminals": {"IN": "vin", "OUT": "out", "GND": "0"},
        "components": ["Vin vin 0 1", "Rload out 0 1000"],
        "stop": 0.001,
        "step": 1e-6,
        "measurement": {
            "operation": "mean",
            "signal": "I(Vin)",
            "start": 0.0009,
            "end": 0.001,
            "absolute": True,
        },
        "condition_evidence": "TEST_FIXTURE: synthetic current bench.",
    }
    with pytest.raises(ValueError, match="current polarity is cited"):
        validate_plan(
            {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": data}]},
            [req],
            tuple(data["terminals"]),
            {},
        )
