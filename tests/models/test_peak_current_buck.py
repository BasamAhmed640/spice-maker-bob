"""The switching-buck seed must be narrow, cited and deterministic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.models.buck_switching import _BODY, TemplateSeedError, seed_from_spec


def _row(
    name: str,
    statement: str,
    *,
    unit: str = "",
    typ: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    excerpt: str = "cited value",
    page: int | None = 5,
    req_class: str = "TYPICAL_VALUE",
) -> Characteristic:
    return Characteristic(
        char_id=name,
        statement=statement,
        unit=unit,
        min_value=minimum,
        max_value=maximum,
        typ_value=typ,
        target=typ,
        source_page=page,
        excerpt=excerpt,
        req_class=req_class,
        probe=None,
        probe_params={},
        not_testable_reason="seed contract test",
    )


_PORTS = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "PowerPAD")


def _spec(*rows: Characteristic, ports: tuple[str, ...] = _PORTS) -> SpecSet:
    return SpecSet(
        part="DEMO_BUCK",
        subckt="DEMO_BUCK",
        doc_id="fixture-document",
        characteristics=tuple(rows),
        pin_map=tuple(
            {"name": port, "physical_pin": index + 1, "direction": "input"}
            for index, port in enumerate(ports)
        ),
    )


def _basis() -> tuple[Characteristic, ...]:
    return (
        _row(
            "R_FSW",
            "Buck switching frequency",
            unit="Hz",
            typ=800_000,
            excerpt="Switching frequency 800 kHz",
        ),
        _row("R_VREF", "Reference voltage", unit="V", typ=0.78, excerpt="Reference voltage 0.78 V"),
        _row(
            "R_ILIM",
            "Switch current limit",
            unit="A",
            minimum=3.0,
            maximum=5.0,
            excerpt="Switch current limit 3.0 to 5.0 A",
        ),
        _row(
            "R_SW_CURRENT_TO_COMP",
            "Switch current to COMP gain",
            unit="A/V",
            typ=10.0,
            excerpt="Switch-current gain 10 A/V",
        ),
    )


def test_seed_preserves_physical_order_and_records_cited_and_default_values(tmp_path):
    spec = _spec(*_basis())
    seed = seed_from_spec(spec)
    assert seed is not None
    assert seed.ports == tuple(port.upper() for port in _PORTS)
    assert seed.library_text.startswith("* Reduced peak-current-mode buck")
    assert ".subckt DEMO_BUCK BOOT VIN EN SS VSENSE COMP GND PH POWERPAD" in seed.library_text
    assert "RPOWERPAD_leak POWERPAD GND 1G" in seed.library_text
    assert "RPOWERPAD POWERPAD GND 1m" not in seed.library_text
    assert "Bchk_powerpad chk_powerpad GND V=if(abs(V(POWERPAD,GND))>0.1,1,0)" in seed.library_text
    parameters = {item.name: item for item in seed.parameters}
    assert parameters["FSW"].value == 800_000
    assert parameters["FSW"].origin == "cited_row"
    assert parameters["ILIM"].value == 4.0
    assert parameters["ILIM"].origin == "derived_from_bounds"
    assert parameters["RON"].origin == "template_default"
    assert parameters["RON"].row_id is None
    assert seed.write(tmp_path / "DEMO_BUCK.lib").read_text(encoding="utf-8") == seed.library_text
    assert seed_from_spec(spec).library_text == seed.library_text
    payload = json.loads(seed.to_json())
    assert payload["verdict"] == "UNJUDGED"
    assert payload["spec_digest"] == spec.digest()
    assert payload["contract_sha256"] == seed.contract_sha256


def test_sw_is_the_stable_default_and_invalid_modes_are_refused():
    # The switching topology is a frozen regression boundary while AVG is added.
    assert hashlib.sha256(_BODY.encode()).hexdigest() == (
        "25625b851f675f71ff153a1b15ae31eea7ade7b2d7bb0c1bfae8e7dcf03310e6"
    )
    spec = _spec(*_basis())
    default = seed_from_spec(spec)
    explicit = seed_from_spec(spec, mode="SW")
    assert default is not None and explicit is not None
    assert default.library_text == explicit.library_text
    assert default.mode == explicit.mode == "SW"
    assert "Vclk1 clk1 GND PULSE" in default.library_text
    assert "Shs swin PH q GND SWHS" in default.library_text
    assert "L_EXT" not in default.library_text
    with pytest.raises(TemplateSeedError, match="invalid_mode"):
        seed_from_spec(spec, mode="WRONG")


def test_avg_uses_identical_physical_pins_and_cited_parameters_with_separate_bench_l():
    spec = _spec(*_basis())
    sw = seed_from_spec(spec, mode="SW")
    avg = seed_from_spec(spec, mode="AVG")
    assert sw is not None and avg is not None
    assert avg.ports == sw.ports
    assert avg.parameters == sw.parameters
    assert avg.contract_sha256 == sw.contract_sha256
    assert avg.spec_digest == sw.spec_digest
    assert f".subckt {spec.subckt} {' '.join(sw.ports)}" in avg.library_text
    assert "Bss VIN SS I={ISS}" in avg.library_text
    assert "Bea GND COMP I=limit({EAGM}" in avg.library_text
    assert "Bipk ipk GND V=limit({GMCS}" in avg.library_text
    assert "Bavgph phsrc GND V=limit(" in avg.library_text
    assert "Bavgin VIN GND I=V(duty,GND)*max(I(Vavgsns),0)" in avg.library_text
    assert "Vavgsns phsense PH 0" in avg.library_text
    assert "Bavgis GND isense I=I(Vavgsns)" in avg.library_text
    assert "Bduty duty GND V=limit(V(di,GND)+0.08*" in avg.library_text
    assert ".param L_EXT=2.5e-06" in avg.library_text
    assert "Vclk1 clk1 GND PULSE" not in avg.library_text
    assert "Aeco COMP GND" not in avg.library_text
    assert "Shs swin PH q GND SWHS" not in avg.library_text
    bench = avg.payload()["external_bench_parameters"]
    assert bench == [
        {
            "name": "L_EXT",
            "default": 2.5e-6,
            "unit": "H",
            "origin": "synthetic_bench_default",
            "instance_override": True,
        }
    ]
    assert avg.payload()["mode"] == "AVG"
    assert avg.payload()["verdict"] == "UNJUDGED"


def test_uncited_numeric_row_does_not_become_a_cited_parameter():
    bad_ref = replace(_basis()[1], source_page=None, typ_value=2.4)
    seed = seed_from_spec(_spec(_basis()[0], bad_ref, *_basis()[2:]))
    assert seed is not None
    ref = next(item for item in seed.parameters if item.name == "VREF")
    assert ref.value == 0.8
    assert ref.origin == "template_default"


def test_failed_citation_is_excluded_from_parameter_provenance():
    spec = _spec(*_basis())
    seed = seed_from_spec(spec, unverified={"R_VREF"})
    assert seed is not None
    ref = next(item for item in seed.parameters if item.name == "VREF")
    assert ref.value == 0.8
    assert ref.origin == "template_default"
    assert ref.row_id is None


def test_stress_rating_is_not_used_as_a_parameter():
    stress = _row(
        "R_VREF", "Absolute maximum VREF stress", unit="V", typ=9.0, req_class="ABSOLUTE_MAXIMUM"
    )
    seed = seed_from_spec(_spec(_basis()[0], stress, *_basis()[2:]))
    assert seed is not None
    ref = next(item for item in seed.parameters if item.name == "VREF")
    assert ref.value == 0.8
    assert ref.origin == "template_default"


def test_other_topology_or_no_buck_evidence_does_not_seed():
    assert seed_from_spec(_spec(*_basis(), ports=("VIN", "EN", "GND", "VOUT"))) is None
    assert seed_from_spec(_spec(_row("R", "A voltage reference"))) is None


def test_impossible_cited_duty_is_refused():
    bad = _row("R_DUTY_MAX", "Maximum duty", unit="%", maximum=130.0)
    with pytest.raises(TemplateSeedError, match="invalid_duty"):
        seed_from_spec(_spec(*_basis(), bad))


def test_foldback_literal_requires_actual_cited_exerpt():
    fold = _row(
        "R_SW_FREQ_VSENSE_GE_0P6",
        "Switching frequency is full above 0.6 V",
        excerpt="Frequency changes with feedback voltage",
    )
    seed = seed_from_spec(_spec(*_basis(), fold))
    assert seed is not None
    fold6 = next(item for item in seed.parameters if item.name == "FOLD6")
    assert fold6.origin == "template_default"
