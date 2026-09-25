from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from boardmodeler.authoring.circuit_probe import CircuitRecipe, make_probe
from boardmodeler.authoring.probes import ProbeError
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.domain.records import Limit, RelativeLimit
from boardmodeler.requirements.model import normalize_unit, scale_factor, validate_requirement


def recipe():
    return {
        "purpose": "TEST_FIXTURE controlled gain",
        "unit": "V",
        "terminals": {"IN": "drive", "OUT": "response", "GND": "0"},
        "components": ["Vin drive 0 0.5", "Rload response 0 1000"],
        "stop": 0.001,
        "step": 1e-6,
        "measurement": {
            "operation": "mean",
            "signal": "V(response)",
            "start": 0.0009,
            "end": 0.001,
        },
        "condition_evidence": "TEST_FIXTURE: 0.5 V input and 1 kohm load; not device data.",
    }


@pytest.mark.parametrize(
    "line",
    [
        ".include stolen.lib",
        "Vbad x 0 1\n.end",
        "Xevil a b OTHER",
        "Vbad x 0 1; .include secret",
        "+ continuation",
    ],
)
def test_fixture_rejects_directives_and_multiline_injection(line):
    data = recipe()
    data["components"].append(line)
    with pytest.raises(ValueError, match="unsupported fixture"):
        CircuitRecipe.model_validate(data)


def test_measurement_cannot_erase_device_response():
    data = recipe()
    data["measurement"]["scale"] = 0
    with pytest.raises(ValueError, match="zero scale"):
        CircuitRecipe.model_validate(data)


def test_measurement_resolves_physical_pin_to_connected_node():
    data = recipe()
    data["measurement"]["signal"] = "V(OUT,GND)"
    parsed = CircuitRecipe.model_validate(data)
    assert parsed.measurement.signal == "V(response,0)"
    data["measurement"]["signal"] = "V(does_not_exist)"
    with pytest.raises(ValueError, match="unconnected node"):
        CircuitRecipe.model_validate(data)


def test_floating_ground_sense_node_is_not_ltspices_reserved_ground_alias():
    data = recipe()
    data["terminals"]["GND"] = "gnd"
    data["components"].append("Rsense gnd 0 100")
    data["measurement"]["signal"] = "V(gnd)"
    parsed = CircuitRecipe.model_validate(data)
    assert parsed.terminals["GND"] == "bm_fixture_ground"
    assert parsed.measurement.signal == "V(bm_fixture_ground)"
    assert "Rsense bm_fixture_ground 0 100" in parsed.components


@pytest.mark.ltspice
def test_operating_point_hint_does_not_force_a_passing_output(tmp_path: Path, ltspice_exe: Path):
    from types import SimpleNamespace

    from boardmodeler.authoring.model_reference import normalize_ground_reference
    from boardmodeler.simulation.ltspice import run_batch

    model = tmp_path / "wrong.lib"
    model.write_text(
        "* TEST_FIXTURE: wrong 1 V regulator\n.subckt DUT IN OUT GND\nVwrong OUT GND 1\nRin IN GND 1Meg\n.ends DUT\n"
    )
    pins = [
        {"name": "OUT", "function": "Regulated output voltage"},
        {"name": "GND", "function": "Common ground"},
    ]
    spec = SimpleNamespace(
        pin_map=pins,
        covered=lambda: [SimpleNamespace(probe_recipe={"operating_point": {"VOUT_NOM": 3.3}})],
    )
    assert normalize_ground_reference(model, pins, tmp_path / "evidence", spec=spec)
    assert ".nodeset V(OUT,GND)=3.3" in model.read_text()
    probe = make_probe(recipe())
    deck = tmp_path / "hint.cir"
    deck.write_text(probe.render(model_lib=model, subckt="DUT", params={}))
    observed = run_batch(ltspice_exe, deck, tmp_path, timeout_s=20)
    assert observed.raw_path
    assert probe.measure(observed.raw_path, {})["recipe_value"] == pytest.approx(1)


def test_delay_requires_both_explicit_thresholds():
    data = recipe()
    data["measurement"].update(operation="delay", trigger="V(drive)", trigger_level=0.25)
    with pytest.raises(ValueError, match="explicit output level"):
        CircuitRecipe.model_validate(data)
    data["measurement"]["level"] = 0
    assert CircuitRecipe.model_validate(data).measurement.level == 0


def test_declared_vm_repairs_legacy_missing_threshold_without_guessing():
    from types import SimpleNamespace

    from boardmodeler.authoring.standard_fixtures import declared_delay_threshold

    data = recipe()
    data["measurement"].update(operation="delay", trigger="V(drive)", trigger_level=1.5)
    req = SimpleNamespace(
        statement="Propagation delay", conditions=[SimpleNamespace(text="VM = 1.5 V")]
    )
    fixed = declared_delay_threshold(data, req)
    assert fixed["measurement"]["level"] == 1.5
    assert "level" not in data["measurement"]
    req.conditions = []
    assert declared_delay_threshold(data, req) is data


def test_generic_voltage_gain_is_not_rewritten_as_an_opamp():
    from boardmodeler.authoring.test_planner import _ac_open_loop

    data = recipe()
    data.update(analysis="ac")
    data["measurement"].update(operation="gain", reference="V(drive)", start=1, end=10)
    original = CircuitRecipe.model_validate(data)
    assert _ac_open_loop(original, [{"function": "signal input"}]) is original


def test_output_swing_compiler_needs_documented_load_reference():
    from types import SimpleNamespace

    from boardmodeler.authoring.standard_fixtures import opamp_output_swing

    req = SimpleNamespace(
        statement="Output swing from supply rails, VS = 5 V, RL = 10 kohm",
        conditions=[],
        evidence=[],
    )
    assert opamp_output_swing(req, []) is None


def test_measurement_window_and_resource_budget_are_bounded():
    data = recipe()
    data["measurement"]["end"] = 1
    with pytest.raises(ValueError, match="beyond"):
        CircuitRecipe.model_validate(data)
    data = recipe()
    data["step"] = 1e-15
    with pytest.raises(ValueError, match="intervals"):
        CircuitRecipe.model_validate(data)


@pytest.mark.parametrize("operation", ["frequency", "delay"])
def test_frequency_uses_consecutive_device_edges_in_frozen_window(monkeypatch, operation):
    data = recipe()
    data.update(unit="Hz", stop=20e-6, step=1e-8)
    data["measurement"] = {
        "operation": operation,
        "signal": "V(response)",
        "start": 5e-6,
        "end": 15e-6,
        "level": 0.5,
    }
    if operation == "delay":
        data["measurement"].update(trigger="V(response)", trigger_level=0.5)
    axis = np.linspace(0, 20e-6, 2001)
    wave = np.sin(2 * np.pi * 1e6 * axis)
    raw = SimpleNamespace(
        data=np.column_stack((axis, wave)),
        complex_data=False,
        column=lambda signal: wave,
    )
    monkeypatch.setattr("boardmodeler.simulation.raw.read_raw", lambda _: raw)
    assert make_probe(data).measure(Path("synthetic.raw"), {})["recipe_value"] == pytest.approx(
        1e6, rel=0.001
    )


def test_frequency_without_two_edges_is_unknown(monkeypatch):
    data = recipe()
    data.update(unit="Hz", stop=20e-6, step=1e-8)
    data["measurement"] = {
        "operation": "frequency",
        "signal": "V(response)",
        "start": 5e-6,
        "end": 15e-6,
        "level": 0.5,
    }
    axis = np.linspace(0, 20e-6, 2001)
    wave = np.zeros_like(axis)
    raw = SimpleNamespace(
        data=np.column_stack((axis, wave)), complex_data=False, column=lambda signal: wave
    )
    monkeypatch.setattr("boardmodeler.simulation.raw.read_raw", lambda _: raw)
    with pytest.raises(ProbeError, match="recipe_frequency_edges_missing"):
        make_probe(data).measure(Path("synthetic.raw"), {})


@pytest.mark.parametrize(
    ("unit", "base", "factor"),
    [
        ("nV/√Hz", "V/sqrt(Hz)", 1e-9),
        ("pA/√Hz", "A/sqrt(Hz)", 1e-12),
        ("µV/°C", "V/C", 1e-6),
        ("V/mV", "V/V", 1e3),
    ],
)
def test_datasheet_noise_and_drift_dimensions(unit, base, factor):
    assert normalize_unit(unit) == base
    assert scale_factor(unit, base) == pytest.approx(factor)


def test_relative_limits_preserve_formula_and_evaluate_only_explicit_point(tmp_path):
    from tests.requirements.test_model import make_requirement

    req = make_requirement(
        limits=Limit(unit="V", min_relative=RelativeLimit(parameter="VCC", factor=1, offset=-0.1))
    )
    assert not [issue for issue in validate_requirement(req) if issue.severity == "error"]
    requirements = tmp_path / "requirements.json"
    requirements.write_text(json.dumps({"requirements": [json.loads(req.model_dump_json())]}))
    bindings = tmp_path / "bindings.json"
    data = recipe()
    data["operating_point"] = {"VCC": 3.3}
    entries = {
        "bindings": [
            {"req_id": req.req_id, "probe": "circuit_measurement", "params": {}, "recipe": data}
        ]
    }
    bindings.write_text(json.dumps(entries))
    spec = load_tps54320_spec(requirements, bindings, part="FIXTURE", subckt="FIXTURE")
    assert spec.characteristics[0].min_value == pytest.approx(3.2)
    assert spec.characteristics[0].relative_limits["min"]["parameter"] == "VCC"
    data["operating_point"] = {}
    bindings.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="explicit VCC"):
        load_tps54320_spec(requirements, bindings, part="FIXTURE", subckt="FIXTURE")


@pytest.mark.ltspice
def test_real_fixture_measures_dut_and_detects_broken_gain(tmp_path: Path, ltspice_exe: Path):
    from boardmodeler.simulation.ltspice import run_batch

    probe = make_probe(recipe())
    observed = []
    for gain in (2, 0.2):
        folder = tmp_path / str(gain)
        folder.mkdir()
        model = folder / "model.lib"
        model.write_text(
            f"* TEST_FIXTURE, not device data\n.subckt DUT IN OUT GND\nEgain OUT GND IN GND {gain}\n.ends DUT\n"
        )
        deck = folder / "deck.cir"
        deck.write_text(probe.render(model_lib=model, subckt="DUT", params={}))
        result = run_batch(ltspice_exe, deck, folder, timeout_s=30)
        assert result.raw_path and result.raw_path.is_file()
        observed.append(probe.measure(result.raw_path, {})["recipe_value"])
    assert observed == pytest.approx([1, 0.1], rel=1e-5)
    assert abs(observed[1] - 1) > 0.5  # Same fixture rejects the incorrect device response.


def test_physical_pin_contract_rejects_invented_terminal(tmp_path):
    model = tmp_path / "model.lib"
    model.write_text(".subckt DUT IN OUT GND EXTRA\n.ends DUT\n")
    with pytest.raises(ProbeError, match="physical_pin_contract"):
        make_probe(recipe()).render(model_lib=model, subckt="DUT", params={})


@pytest.mark.parametrize("minus", ["-", "\u2212", "\u2013", "\u2014"])
def test_pin_polarity_survives_pdf_minus_variants(minus):
    from boardmodeler.authoring.pin_roles import terminal_name

    assert terminal_name({"name": f"IN1{minus}"}) == "IN1M"


def test_operating_envelope_is_not_an_output_voltage_spec():
    from boardmodeler.authoring.test_planner import validate_plan
    from tests.requirements.test_model import make_requirement

    req = make_requirement(statement="Recommended operating input supply voltage 1.6 V to 6 V")
    result = validate_plan(
        {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": recipe()}]},
        [req],
        ("IN", "OUT", "GND"),
        {},
    )
    assert result[0]["probe"] is None
    assert "operating envelope" in result[0]["not_testable_reason"]


def test_planner_cannot_measure_a_forced_source_as_the_dut():
    from boardmodeler.authoring.test_planner import validate_plan
    from tests.requirements.test_model import make_requirement

    req = make_requirement(section="Electrical Characteristics")
    data = recipe()
    data["measurement"]["signal"] = "V(drive)"
    with pytest.raises(ValueError, match="forced independent voltage"):
        validate_plan(
            {"bindings": [{"req_id": req.req_id, "probe": "circuit_measurement", "recipe": data}]},
            [req],
            ("IN", "OUT", "GND"),
            {},
        )


def test_one_invalid_fixture_does_not_erase_other_source_rows():
    from boardmodeler.authoring.test_planner import validate_partial_plan
    from tests.requirements.test_model import make_requirement

    good = make_requirement(section="Electrical Characteristics")
    bad = good.model_copy(update={"req_id": "bad"})
    invalid = recipe()
    invalid["measurement"].update(operation="delay", trigger="V(drive)")
    rows = validate_partial_plan(
        {
            "bindings": [
                {"req_id": good.req_id, "probe": "circuit_measurement", "recipe": recipe()},
                {"req_id": bad.req_id, "probe": "circuit_measurement", "recipe": invalid},
            ]
        },
        [good, bad],
        ("IN", "OUT", "GND"),
        {},
    )
    assert rows[0]["probe"] == "circuit_measurement"
    assert rows[1]["req_id"] == "bad" and rows[1]["probe"] is None
    assert "invalid test fixture" in rows[1]["not_testable_reason"]


@pytest.mark.ltspice
def test_open_loop_ac_compiler_measures_known_gbw(tmp_path: Path, ltspice_exe: Path):
    from boardmodeler.authoring.test_planner import _ac_open_loop
    from boardmodeler.simulation.ltspice import run_batch

    data = recipe()
    data.update(
        unit="Hz",
        analysis="ac",
        terminals={"IP": "ip", "IM": "out", "OUT": "out"},
        components=["Vin ip 0 DC 0 AC 1", "Rload out 0 1e12"],
        measurement={
            "operation": "unity_frequency",
            "signal": "V(out)",
            "reference": "V(ip)",
            "start": 1,
            "end": 1e8,
        },
    )
    pins = [
        {"name": "IP", "function": "Noninverting input"},
        {"name": "IM", "function": "Inverting input"},
        {"name": "OUT", "function": "Output"},
    ]
    fixed = _ac_open_loop(CircuitRecipe.model_validate(data), pins)
    assert fixed.measurement.reference == "V(ip,bm_ac_minus)"
    model = tmp_path / "fixture.lib"
    model.write_text(
        "* TEST_FIXTURE: A0=100000, pole=10 Hz\n.subckt DUT IP IM OUT\nEgain pole 0 IP IM 100000\nRpole pole OUT 1000\nCpole OUT 0 15.915494309u\n.ends DUT\n"
    )
    probe = make_probe(fixed.model_dump())
    deck = tmp_path / "gbw.cir"
    deck.write_text(probe.render(model_lib=model, subckt="DUT", params={}))
    result = run_batch(ltspice_exe, deck, tmp_path, timeout_s=30)
    assert result.raw_path
    assert probe.measure(result.raw_path, {})["recipe_value"] == pytest.approx(1e6, rel=0.002)
