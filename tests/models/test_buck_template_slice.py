"""The reusable buck template slice: pin roles, cited mapping, gate wiring and its bench."""

from __future__ import annotations

import re

import pytest

from boardmodeler.authoring.buck_fixtures import current_limit_recipe, soft_start_time
from boardmodeler.authoring.circuit_probe import CircuitRecipe
from boardmodeler.authoring.convergence import lint_library
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.authoring.test_planner import _buck_fixture_issue
from boardmodeler.models.buck_switching import _load_contract, match_pins, seed_from_spec

ROLES = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH")


def _pin(name: str, number: int, direction: str) -> dict:
    return {
        "name": name,
        "physical_pin": str(number),
        "direction": direction,
        "polarity": "not_applicable",
        "output_topology": "not_applicable",
        "connection_requirement": "required",
        "function": name,
    }


def _char(char_id: str, statement: str, unit: str, **values: float) -> Characteristic:
    return Characteristic(
        char_id=char_id,
        statement=statement,
        excerpt=statement,
        source_page=5,
        unit=unit,
        req_class="DOCUMENTED_LIMIT",
        target=values.get("typ_value", values.get("min_value")),
        min_value=values.get("min_value"),
        typ_value=values.get("typ_value"),
        max_value=values.get("max_value"),
        probe=None,
        probe_params={},
        conditions=(),
        not_testable_reason="unit test row",
    )


def _spec(pin_names: tuple[str, ...]) -> SpecSet:
    """A small buck spec with generic row ids (as a fresh extraction produces)."""
    rows = (
        _char(
            "B002_REQ_007",
            "Voltage reference is 0.772 V minimum, 0.8 V typical and 0.828 V maximum.",
            "V",
            min_value=0.772,
            typ_value=0.8,
            max_value=0.828,
        ),
        _char(
            "B002_REQ_020",
            "Switching frequency is 570 kHz typical at VIN = 12 V.",
            "Hz",
            typ_value=570e3,
        ),
        _char(
            "B002_REQ_016",
            "Current limit threshold is 3.5 A minimum and 5.8 A typical at VIN = 12 V.",
            "A",
            min_value=3.5,
            typ_value=5.8,
        ),
        _char(
            "B002_REQ_018",
            "Slow-start charge current is 2 μA typical at V(SS) = 0.4 V.",
            "A",
            typ_value=2e-6,
        ),
        _char(
            "B002_REQ_001",
            "The internal undervoltage lockout threshold is 3.5 V rising.",
            "V",
            min_value=3.5,
        ),
        _char(
            "B002_REQ_027",
            "The boot capacitor voltage is monitored by an UVLO circuit at 2.1 V.",
            "V",
            typ_value=2.1,
        ),
    )
    pins = tuple(_pin(name, index + 1, "input") for index, name in enumerate(pin_names))
    return SpecSet(
        part="BUCKX",
        subckt="BUCKX",
        doc_id="DOC_TEST",
        characteristics=rows,
        pin_map=pins,
    )


def test_aliases_map_a_differently_named_buck_onto_the_roles():
    match = match_pins(("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND"))
    assert match.ok, match.reason
    assert match.roles == {
        "BOOT": "BST",
        "VIN": "VIN",
        "EN": "EN",
        "SS": "SS_TR",
        "VSENSE": "FB",
        "COMP": "COMP",
        "GND": "AGND",
        "PH": "SW",
    }
    assert match.ground_ties == ("PGND",)
    assert match.required_ground_connections == ("PGND",)


@pytest.mark.parametrize(
    ("ports", "reason"),
    [
        (
            ("BOOT", "VIN", "EN", "RT", "VSENSE", "COMP", "GND", "PH", "SS"),
            "pin_role_unsupported: RT",
        ),
        (("BOOT", "VIN", "EN", "VSENSE", "COMP", "GND", "PH"), "pin_role_missing: SS"),
        (("BOOT", "VIN", "EN", "SS", "FB", "VSENSE", "COMP", "GND", "PH"), "pin_role_duplicate"),
        (
            ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "UNKNOWNPAD"),
            "pin_role_unsupported: UNKNOWNPAD",
        ),
    ],
)
def test_a_pinout_the_template_cannot_represent_is_refused_with_its_reason(ports, reason):
    match = match_pins(ports)
    assert not match.ok and match.reason.startswith(reason)


def test_generic_row_ids_are_mapped_by_statement_and_unit():
    """A fresh extraction's rows (B002_REQ_nnn) used to fall back to all defaults."""
    seed = seed_from_spec(_spec(ROLES))
    assert seed is not None
    params = {p.name: p for p in seed.parameters}
    assert (params["VREF"].origin, params["VREF"].row_id, params["VREF"].value) == (
        "cited_row",
        "B002_REQ_007",
        0.8,
    )
    assert (params["FSW"].origin, params["FSW"].value) == ("cited_row", 570e3)
    assert (params["ILIM"].origin, params["ILIM"].value) == ("cited_row", 5.8)
    assert (params["ISS"].origin, params["ISS"].value) == ("cited_row", 2e-6)
    # The BOOT UVLO row must not be read as the VIN UVLO threshold.
    assert params["UVTH"].row_id == "B002_REQ_001"
    assert params["ENHYS"].origin == "template_default"


def test_the_rendered_model_keeps_the_pad_external_with_only_a_convergence_leak():
    names = ("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND")
    seed = seed_from_spec(_spec(names))
    assert seed is not None
    text = seed.library_text
    assert re.search(r"^\.subckt BUCKX BST VIN EN SS_TR FB COMP AGND SW PGND$", text, re.M)
    assert "RPGND_leak PGND AGND 1G" in text
    assert "RPGND PGND AGND 1m" not in text
    active_pad_lines = [
        line for line in text.splitlines() if "PGND" in line and not line.startswith("*")
    ]
    assert active_pad_lines == [
        ".subckt BUCKX BST VIN EN SS_TR FB COMP AGND SW PGND",
        "RPGND_leak PGND AGND 1G",
        "Bchk_pgnd chk_pgnd AGND V=if(abs(V(PGND,AGND))>0.1,1,0)",
    ]
    assert not re.search(r"\b(?:PH|VSENSE|BOOT)\b", text.split("\n", 3)[3].split(".ends")[0])


def test_avg_alias_pins_keep_external_pad_and_use_the_same_physical_order():
    names = ("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND")
    spec = _spec(names)
    sw = seed_from_spec(spec, mode="SW")
    avg = seed_from_spec(spec, mode="AVG")
    assert sw is not None and avg is not None
    assert avg.ports == sw.ports == tuple(name.upper() for name in names)
    assert avg.parameters == sw.parameters
    assert ".subckt BUCKX BST VIN EN SS_TR FB COMP AGND SW PGND" in avg.library_text
    assert "RPGND_leak PGND AGND 1G" in avg.library_text
    assert "Bchk_pgnd chk_pgnd AGND V=if(abs(V(PGND,AGND))>0.1,1,0)" in avg.library_text
    assert "Bavgph phsrc AGND V=limit(" in avg.library_text
    assert "Vavgsns phsense SW 0" in avg.library_text
    assert "RPGND PGND AGND 1m" not in avg.library_text


def test_a_part_without_an_exposed_pad_keeps_all_eight_ports_and_needs_no_pad_leak():
    match = match_pins(ROLES)
    assert match.ok and match.required_ground_connections == ()
    seed = seed_from_spec(_spec(ROLES))
    assert seed is not None
    assert ".subckt BUCKX BOOT VIN EN SS VSENSE COMP GND PH\n" in seed.library_text
    assert "_leak" not in seed.library_text


def test_every_logic_gate_references_the_models_own_ground():
    seed = seed_from_spec(_spec(ROLES))
    assert seed is not None
    gates = [line.split() for line in seed.library_text.splitlines() if line.startswith("A")]
    assert gates and all(words[8] == "GND" and "0" not in words[1:9] for words in gates)
    assert not [f for f in lint_library(seed.library_text) if f.code == "a_device_input_ground"]


def test_the_contract_states_what_the_template_does_and_does_not_model():
    contract = _load_contract()
    assert "ground_tie_pins" not in contract
    rule = contract["required_connections"][0]
    assert rule["id"] == "exposed_pad_to_gnd"
    assert rule["to_role"] == "GND"
    assert rule["location"] == "PCB" and rule["applies_when"] == "pin_present"
    assert rule["citation_required"] is True
    assert "POWERPAD" in rule["pin_aliases"] and "PGND" in rule["pin_aliases"]
    assert "source" not in rule
    assert any("current limit" in item for item in contract["supported_behaviors"])
    assert any("temperature" in item for item in contract["unsupported_behaviors"])


def test_the_current_limit_bench_passes_the_pre_freeze_rules_and_starts_after_soft_start():
    match = match_pins(("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND"))
    recipe = current_limit_recipe(
        match.roles,
        match.ground_ties,
        vin=12.0,
        charge_current=2e-6,
        reference_min=0.772,
        reference_typ=0.8,
        limit_high=6.5,
        evidence="Current limit threshold is 4.2 A minimum and 6.5 A maximum.",
    )
    validated = CircuitRecipe.model_validate(recipe)
    assert validated.terminals["SW"] == "ph" and validated.terminals["PGND"] == "0"
    assert validated.measurement.start >= soft_start_time(10e-9, 2e-6, 0.772)
    rows = (
        ("Slow-start charge current is 2 μA typical.", {"typ": 2.0, "unit": "μA"}),
        ("Voltage reference is 0.772 V minimum.", {"min": 0.772, "unit": "V"}),
    )
    assert _buck_fixture_issue(validated, rows, "Current limit threshold") is None
    # The same bench moved back to the saved 0.5 ms window is refused.
    early_payload = {**recipe, "measurement": {**recipe["measurement"], "start": 5e-4}}
    early = CircuitRecipe.model_validate(early_payload)
    assert (_buck_fixture_issue(early, rows, "Current limit threshold") or "").startswith(
        "buck_fixture_soft_start"
    )
