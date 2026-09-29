"""Independent physical buck benches use cited rows, never the generated model."""

from __future__ import annotations

from copy import deepcopy

import pytest

from boardmodeler.authoring.buck_fixtures import complete_buck_bindings
from boardmodeler.authoring.test_planner import validate_plan
from boardmodeler.domain.enums import RequirementClass
from boardmodeler.domain.records import Condition, Limit
from tests.requirements.test_model import make_evidence, make_requirement


def _row(req_id, statement, limits):
    return make_requirement(
        req_id=req_id,
        statement=statement,
        excerpt=statement,
        limits=limits,
        section="TEST_FIXTURE electrical characteristics",
    ).model_copy(update={"citation_verified": True})


ROWS = [
    _row("R1", "Voltage reference", Limit(min=0.772, typ=0.8, max=0.828, unit="V")),
    _row("R2", "Current-limit threshold at VIN = 12 V", Limit(min=3.5, max=5.8, unit="A")),
    _row("R3", "Slow start charge current", Limit(typ=2, unit="uA")),
    _row("R4", "An external catch diode is required between PH and GND", None),
]
PIN_NAMES = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH")
PINS = [{"name": name, "physical_pin": str(i + 1)} for i, name in enumerate(PIN_NAMES)]


def _entries():
    return [
        {"req_id": row.req_id, "probe": None, "not_testable_reason": "No independent fixture"}
        for row in ROWS
    ]


def _bind(rows=ROWS, entries=None, *, pin_map=PINS, unverified=()):
    return complete_buck_bindings(
        rows, pin_map, _entries() if entries is None else entries, unverified=unverified
    )


def _validate(row, recipe, rows=ROWS):
    return validate_plan(
        {"bindings": [{"req_id": row.req_id, "probe": "circuit_measurement", "recipe": recipe}]},
        [row],
        tuple(recipe["terminals"]),
        {},
        PINS,
        context=[r.model_dump(mode="json", by_alias=True) for r in rows],
    )


def test_reference_and_limit_use_loaded_power_stages_after_cited_ss_charging():
    before = [row.model_dump_json() for row in ROWS]
    entries = _bind()
    assert [entry["probe"] for entry in entries] == [
        "circuit_measurement",
        "circuit_measurement",
        None,
        None,
    ]
    ref, limit = (entry["recipe"] for entry in entries[:2])
    for recipe in (ref, limit):
        assert recipe["measurement"]["start"] == pytest.approx(0.00514)
        assert recipe["measurement"]["end"] == pytest.approx(0.00714)
        assert set(recipe["terminals"]) == set(PIN_NAMES)
        assert "Dcatch 0 ph BM_CATCH" in recipe["components"]
        assert "Cboot boot ph 0.1u" in recipe["components"]
        assert "C2 ss 0 1e-08" in recipe["components"]
        assert any(line.startswith("Rload vout 0 ") for line in recipe["components"])
        assert not any(line.startswith(("Iload", "Bload", "Vref")) for line in recipe["components"])
        assert "R1 (DOC_synthetic_regulator page 0)" in recipe["condition_evidence"]
    assert ref["measurement"]["operation"] == "mean"
    assert ref["measurement"]["signal"] == "V(vsense)"
    assert ref["operating_point"]["RLOAD"] == pytest.approx(3.3 / (3.5 / 4))
    assert limit["measurement"]["operation"] == "max"
    assert limit["measurement"]["signal"] == "V(ph,sw)"
    assert limit["measurement"]["scale"] == 50
    assert limit["operating_point"]["RLOAD"] == pytest.approx(3.3 / (1.5 * 5.8))
    assert [row.model_dump_json() for row in ROWS] == before
    assert _bind() == entries


@pytest.mark.parametrize("index,early", [(0, 0.002), (1, 0.005)])
def test_saved_planner_windows_are_rejected_while_fixed_windows_pass(index, early):
    recipe = _bind()[index]["recipe"]
    assert _validate(ROWS[index], recipe)[0]["probe"] == "circuit_measurement"
    faulty = deepcopy(recipe)
    faulty["measurement"]["start"] = early
    with pytest.raises(ValueError, match=r"buck_fixture_soft_start.*0\.00514 s"):
        _validate(ROWS[index], faulty)


def test_reference_voltage_word_order_cannot_bypass_the_early_window_guard():
    rows = [ROWS[0].model_copy(update={"statement": "Reference voltage"}), *ROWS[1:]]
    recipe = _bind(rows)[0]["recipe"]
    recipe["measurement"]["start"] = 0.002
    with pytest.raises(ValueError, match="buck_fixture_soft_start"):
        _validate(rows[0], recipe, rows)


def test_slower_cited_minimum_charge_current_controls_both_windows():
    rows = [
        *ROWS[:2],
        ROWS[2].model_copy(update={"limits": Limit(min=1, typ=2, unit="uA")}),
        ROWS[3],
    ]
    entries = _bind(rows)
    for entry in entries[:2]:
        assert entry["recipe"]["measurement"]["start"] == pytest.approx(0.00928)


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_missing_or_failed_citations_never_borrow_model_defaults(missing):
    assert _bind([row for i, row in enumerate(ROWS) if i != missing]) == _entries()
    assert _bind(unverified={ROWS[missing].req_id}) == _entries()
    rows = [
        row.model_copy(update={"citation_verified": False}) if i == missing else row
        for i, row in enumerate(ROWS)
    ]
    assert _bind(rows) == _entries()


def test_a_minimum_only_current_limit_does_not_establish_a_guaranteed_overload():
    rows = [ROWS[0], ROWS[1].model_copy(update={"limits": Limit(min=3.5, unit="A")}), *ROWS[2:]]
    assert _bind(rows) == _entries()


def test_other_part_or_document_evidence_and_absolute_maxima_do_not_complete_a_bench():
    for update in (
        {"applies_to": "ANOTHER_PART"},
        {"evidence": [make_evidence(doc_id="ANOTHER_DOCUMENT")]},
        {"req_class": RequirementClass.ABSOLUTE_MAXIMUM},
        {"status": "conflict", "conflicts": ["inconsistent cited charge current"]},
    ):
        rows = [*ROWS[:2], ROWS[2].model_copy(update=update), ROWS[3]]
        assert _bind(rows) == _entries()


def test_conflicting_supply_conditions_are_left_unbound():
    extra = ROWS[1].model_copy(
        update={"req_id": "OTHER_LIMIT", "statement": "Current-limit threshold at VIN = 15 V"}
    )
    entries = _bind([*ROWS, extra])
    assert entries[0]["probe"] is None
    assert entries[1]["recipe"]["operating_point"]["VIN"] == 12


@pytest.mark.parametrize(
    "header", ["VIN = 3.5 V to 28 V", "VIN = 3.5 V - 28 V", "VIN = 3.5 V\u201328 V"]
)
def test_supply_range_does_not_hide_the_rows_explicit_test_point(header):
    rows = [row.model_copy(update={"conditions": [Condition(text=header)]}) for row in ROWS]
    rows[1] = rows[1].model_copy(
        update={
            "statement": "Current-limit threshold",
            "limits": Limit(min=3.5, typ=5.8, unit="A"),
            "conditions": [Condition(text=f"{header} unless otherwise noted. VIN = 12 V.")],
        }
    )
    for entry in _bind(rows)[:2]:
        assert entry["probe"] == "circuit_measurement"
        assert entry["recipe"]["operating_point"]["VIN"] == 12


@pytest.mark.parametrize("points", ["", " VIN = 12 V; VIN = 15 V."])
def test_supply_range_alone_or_contradictory_points_do_not_select_a_fixture(points):
    rows = [
        row.model_copy(update={"conditions": [Condition(text="VIN = 3.5 V to 28 V")]})
        for row in ROWS
    ]
    rows[1] = rows[1].model_copy(
        update={
            "statement": "Current-limit threshold",
            "conditions": [Condition(text="VIN = 3.5 V to 28 V." + points)],
        }
    )
    assert _bind(rows) == _entries()


def test_existing_physical_recipes_are_unchanged_and_generic_probes_are_replaced():
    fixed = _bind()
    assert _bind(entries=fixed) == fixed
    generic = _entries()
    generic[0] = {"req_id": "R1", "probe": "vref", "params": {}}
    generic[1] = {"req_id": "R2", "probe": "current_limit", "params": {}}
    assert _bind(entries=generic) == fixed
    assert _bind(pin_map=[{"name": "OUT", "physical_pin": "1"}]) == _entries()


def test_alias_pin_names_and_an_external_pad_still_connect_every_terminal():
    names = ("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND")
    pins = [{"name": name, "physical_pin": str(i + 1)} for i, name in enumerate(names)]
    for entry in _bind(pin_map=pins)[:2]:
        terminals = entry["recipe"]["terminals"]
        assert set(terminals) == set(names)
        assert terminals["AGND"] == terminals["PGND"] == "0"
        assert terminals["FB"] == "vsense" and terminals["SW"] == "ph"


def test_an_unrelated_opamp_binding_remains_unchanged():
    row = _row("OP1", "Op amp reference voltage", Limit(typ=1.2, unit="V"))
    entries = [{"req_id": row.req_id, "probe": "opamp_offset", "params": {}}]
    assert complete_buck_bindings([row], [{"name": "OUT", "physical_pin": "1"}], entries) == entries
