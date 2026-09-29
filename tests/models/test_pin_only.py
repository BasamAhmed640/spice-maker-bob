"""Pin-only mode: an extracted pin table becomes pins, supply draw, clamps and alarms, and no function."""

from __future__ import annotations

import pytest

from boardmodeler.models.library import subckt_ports
from boardmodeler.models.pin_only import PinOnlyRefusal, build_pin_only


def pin(
    name: str,
    direction: str,
    number: int,
    *,
    topology: str = "unknown",
    requirement: str = "optional",
    domain: str | None = None,
) -> dict[str, object]:
    return {
        "part_id": "X1",
        "physical_pin": str(number),
        "name": name,
        "function": "test pin",
        "polarity": "not_applicable",
        "direction": direction,
        "supply_domain": domain,
        "output_topology": topology,
        "connection_requirement": requirement,
    }


REGULATOR = [
    pin("VIN", "power", 1),
    pin("GND", "ground", 2),
    pin("EN", "input", 3, requirement="required"),
    pin("PG", "output", 4, topology="open_drain"),
    pin("VOUT", "power", 5),
    pin("EP", "ground", 6, requirement="required"),
]


def test_a_regulator_pin_table_becomes_a_model_with_every_pin_a_port_in_table_order() -> None:
    built = build_pin_only("X1", "X1", REGULATOR)
    assert built.ports == ("VIN", "GND", "EN", "PG", "VOUT", "EP")
    assert subckt_ports(built.library_text, "X1") == built.ports
    kinds = {p.port: p.kind for p in built.pins}
    assert kinds == {
        "VIN": "supply",
        "GND": "ground",
        "EN": "input",
        "PG": "output",
        "VOUT": "output",
        "EP": "pad",
    }
    assert built.rail == "VIN" and built.ground == "GND"
    topologies = {p.port: p.topology for p in built.pins if p.kind == "output"}
    assert topologies == {"PG": "open_drain", "VOUT": "push_pull"}


def test_the_same_pin_table_renders_the_same_bytes() -> None:
    assert build_pin_only("X1", "X1", REGULATOR).library_text == (
        build_pin_only("X1", "X1", REGULATOR).library_text
    )


def test_outputs_are_inert_until_commanded_and_the_pad_alarm_is_claimed() -> None:
    built = build_pin_only("X1", "X1", REGULATOR)
    assert "params: LEVEL_PG=-1 LEVEL_VOUT=-1" in built.library_text
    claimed = built.claimed_alarms()
    assert claimed["PG"] == ("ovl",) and claimed["VOUT"] == ("ovl",)
    assert claimed["EP"] == ("tie",)
    assert "EN" not in claimed, "a required input is not given an alarm this mode cannot prove"


def test_a_power_pin_named_like_an_output_is_an_output_and_the_note_says_so() -> None:
    built = build_pin_only("X1", "X1", REGULATOR)
    assert any("named like an output" in note for note in built.notes)
    assert any("no drive strength" in note for note in built.notes)


def test_the_rail_is_the_supply_pin_named_like_one_and_the_others_draw_nothing() -> None:
    pins = [pin("AUX", "power", 1), pin("VIN", "power", 2), pin("GND", "ground", 3)]
    built = build_pin_only("X1", "X1", pins)
    assert built.rail == "VIN"
    draw = {p.port: p.iq for p in built.pins if p.kind == "supply"}
    assert draw == {"AUX": 0.0, "VIN": 1e-6}
    assert any("VIN is the rail" in note for note in built.notes)


def test_a_second_required_ground_gets_the_missing_connection_alarm() -> None:
    pins = [
        pin("VCC", "power", 1),
        pin("GND", "ground", 2),
        pin("AGND", "ground", 3, requirement="required"),
    ]
    built = build_pin_only("X1", "X1", pins)
    assert built.claimed_alarms() == {"AGND": ("tie",)}


def test_an_input_named_like_an_exposed_pad_is_a_pad() -> None:
    pins = [pin("VCC", "power", 1), pin("GND", "ground", 2), pin("PowerPAD", "input", 3)]
    kinds = {p.port: p.kind for p in build_pin_only("X1", "X1", pins).pins}
    assert kinds["POWERPAD"] == "pad"


REFUSALS = {
    "an empty pin table": ([], "pin_only_no_pin_table"),
    "no ground pin": ([pin("VCC", "power", 1), pin("IN", "input", 2)], "pin_only_no_ground"),
    "no supply pin": ([pin("GND", "ground", 1), pin("IN", "input", 2)], "pin_only_no_supply"),
    "two supply domains": (
        [
            pin("VCCA", "power", 1, domain="A side"),
            pin("VCCB", "power", 2, domain="B side"),
            pin("GND", "ground", 3),
        ],
        "pin_only_multiple_rails",
    ),
    "a direction nothing maps": (
        [pin("VCC", "power", 1), pin("GND", "ground", 2), pin("X", "wireless", 3)],
        "pin_only_unknown_direction",
    ),
    "a terminal name twice": (
        [pin("VCC", "power", 1), pin("GND", "ground", 2), pin("gnd", "ground", 3)],
        "pin_only_ambiguous_pins",
    ),
}


@pytest.mark.parametrize("name", list(REFUSALS))
def test_a_pin_table_that_needs_a_guess_is_refused_with_the_reason(name: str) -> None:
    pins, code = REFUSALS[name]
    with pytest.raises(PinOnlyRefusal, match=code):
        build_pin_only("X1", "X1", pins)
