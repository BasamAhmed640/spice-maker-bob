"""The support decision: blocked classes stop every route and an unknown part is never supported."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from tests.models import test_peak_current_buck as peak_tests
from tests.models.op_amp_spec import synthetic_dual_op_amp_spec

from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.support import (
    IMPLEMENTATIONS,
    ORDINARY_FAMILIES,
    ROUTES,
    Implementation,
    decide_support,
    family_ids,
    identify_family,
    identify_family_scored,
)

BLOCKED_PARTS = [
    "STM32F407VGT6",
    "ATmega328P",
    "ATSAMD21G18A-AU",
    "ESP32",
    "ESP8266EX",
    "RP2040",
    "nRF52840",
    "nRF5340",
    "XC7A35T",
    "XC7Z020",
    "iCE40UP5K",
    "10M08SAU169C8G",
    "LPC1768",
    "IMX6ULL",
    "PIC32MX795F512L",
    "TMS320F28335",
    "MSP432P401R",
    "CC2640R2F",
    "STM8S003F3",
    "EFM32GG11",
    "XMC4700",
    "CY8C5888",
    "GD32F103",
    "GW1N-9",
    "ZYNQ-7020",
]
BLOCKED_TITLES = [
    "ACME1 32-bit Microcontroller Datasheet",
    "ACME2 FPGA Family Data Sheet",
    "ACME3 CPLD Family Data Sheet",
    "ACME4 System-on-Chip Technical Reference",
    "ACME5 Application Processor",
    "ACME6 Digital Signal Processor",
    "ACME7 Single-Board Computer",
    "ACME8 system on module",
]
UNKNOWN = [
    ("XYZ-1234", ""),
    ("ACME Widget", "ACME Widget Datasheet"),
    ("QX9", "Quantum Flux Widget"),
    ("", ""),
]


def _buck_spec():
    return peak_tests._spec(*peak_tests._basis())


def _supported_rows():
    row = peak_tests._row
    extra = (
        row(
            "R_EN_THRESHOLD",
            "Enable threshold voltage",
            unit="V",
            typ=1.25,
            excerpt="Enable 1.25 V",
        ),
        row(
            "R_UVLO_VIN",
            "Internal undervoltage lockout threshold",
            unit="V",
            minimum=3.5,
            excerpt="UVLO 3.5 V",
        ),
        row(
            "R_SS_CHARGE",
            "Slow-start charge current",
            unit="A",
            typ=2e-6,
            excerpt="Slow-start 2 uA",
        ),
    )
    rows = (*peak_tests._basis(), *extra)
    bound = ("R_VREF", "R_FSW", "R_ILIM", "R_EN_THRESHOLD", "R_UVLO_VIN", "R_SS_CHARGE")
    return tuple(
        dataclasses.replace(item, probe="circuit_measurement") if item.char_id in bound else item
        for item in rows
    )


@pytest.mark.parametrize("part", BLOCKED_PARTS)
def test_a_blocked_part_number_stops_every_route(part: str) -> None:
    decision = decide_support(part)
    assert decision.state == "blocked_class"
    assert not decision.supported
    for route in ROUTES:
        assert not decision.allows(route)
        assert decision.refusal(route).startswith("unsupported_part_class:")


@pytest.mark.parametrize("title", BLOCKED_TITLES)
def test_a_blocked_datasheet_title_stops_every_route(title: str) -> None:
    decision = decide_support(title.split()[0], title=title)
    assert decision.state == "blocked_class"
    assert not any(decision.allows(route) for route in ROUTES)


def test_a_blocked_class_wins_even_when_the_rows_look_like_a_buck() -> None:
    decision = decide_support("STM32F407", title="step-down converter", spec=_buck_spec())
    assert decision.state == "blocked_class"
    assert decision.implementation is None


@pytest.mark.parametrize(("part", "title"), UNKNOWN)
def test_an_unknown_part_is_never_supported_on_any_route(part: str, title: str) -> None:
    decision = decide_support(part, title=title)
    assert decision.state == "unclassified"
    assert not decision.supported
    for route in ROUTES:
        assert not decision.allows(route)
        assert decision.refusal(route).startswith("unsupported_part: unclassified:")


@pytest.mark.parametrize(
    ("family_id", "phrase"),
    [(fid, phrase) for fid, _label, phrases in ORDINARY_FAMILIES for phrase in phrases],
)
def test_every_written_phrase_identifies_its_own_family(family_id: str, phrase: str) -> None:
    found = identify_family(f"Acme {phrase} data sheet")
    assert found is not None
    if found[0] != family_id:
        # a phrase shared with an earlier row is fine only when that row also lists it
        earlier = next(row for row in ORDINARY_FAMILIES if row[0] == found[0])
        assert any(phrase in other or other in phrase for other in earlier[2])


def test_a_matching_buck_with_defaulted_inputs_and_no_bound_tests_is_not_supported() -> None:
    decision = decide_support("DEMO_BUCK", title="DEMO_BUCK step-down converter", spec=_buck_spec())
    assert decision.state == "unsupported_family"
    assert decision.implementation == "peak_current_buck"
    assert decision.family == "switching_regulator"
    assert any(item == "cited input ISS" for item in decision.missing)
    assert any(item.startswith("independent test for") for item in decision.missing)
    assert not decision.allows("behavioral")
    assert decision.allows("pin_only")
    assert decision.allows("legacy_ai")
    assert decision.refusal("behavioral").startswith("unsupported_part: unsupported_family:")


def test_a_buck_with_cited_inputs_and_independent_tests_is_supported() -> None:
    spec = dataclasses.replace(_buck_spec(), characteristics=_supported_rows())
    decision = decide_support("DEMO_BUCK", title="DEMO_BUCK step-down converter", spec=spec)
    assert decision.state == "supported", decision.missing
    assert decision.supported
    assert decision.missing == ()
    assert all(decision.allows(route) for route in ROUTES)


def test_removing_one_input_or_one_test_withdraws_the_claim() -> None:
    rows = _supported_rows()
    spec = dataclasses.replace(_buck_spec(), characteristics=rows)
    unverified = decide_support("DEMO_BUCK", spec=spec, unverified={"R_EN_THRESHOLD"})
    assert unverified.state == "unsupported_family"
    assert "cited input ENTH" in unverified.missing
    unbound = tuple(
        dataclasses.replace(item, probe=None) if item.char_id == "R_SS_CHARGE" else item
        for item in rows
    )
    decision = decide_support("DEMO_BUCK", spec=dataclasses.replace(spec, characteristics=unbound))
    assert decision.state == "unsupported_family"
    assert "independent test for soft start" in decision.missing


def test_an_ordinary_family_without_an_implementation_is_open_only_to_limited_routes() -> None:
    decision = decide_support("LM358", title="LM358 Dual Operational Amplifiers")
    assert decision.state == "unsupported_family"
    assert decision.family == "amplifier_comparator"
    assert decision.implementation is None
    assert not decision.allows("behavioral")
    assert decision.allows("pin_only")
    assert decision.allows("legacy_ai")
    assert "pins only, no function" in decision.routes["pin_only"].reason


def test_the_three_conditions_are_all_required_with_a_stand_in_implementation() -> None:
    def stand_in(uncited: tuple[str, ...], untested: tuple[str, ...]) -> Implementation:
        return Implementation(
            name="stand_in",
            family="linear_regulator",
            label="stand-in",
            matches=lambda spec, unverified: object(),
            uncited=lambda design: uncited,
            untested=lambda spec: untested,
        )

    spec = _buck_spec()
    cases = (
        ((), (), "supported"),
        (("VOUT",), (), "unsupported_family"),
        ((), ("dropout",), "unsupported_family"),
        (("VOUT",), ("dropout",), "unsupported_family"),
    )
    for uncited, untested, state in cases:
        decision = decide_support("XR1", spec=spec, registry=(stand_in(uncited, untested),))
        assert decision.state == state
        assert len(decision.missing) == len(uncited) + len(untested)


def test_support_is_not_a_pass_and_the_registry_names_real_families() -> None:
    family_ids = {row[0] for row in ORDINARY_FAMILIES}
    assert IMPLEMENTATIONS
    assert all(impl.family in family_ids for impl in IMPLEMENTATIONS)
    assert not any(
        "processor" in row[0] or "microcontroller" in row[0] for row in ORDINARY_FAMILIES
    )


FROZEN_DIR = Path(__file__).resolve().parents[2] / "models" / "T1-tps54332" / "spec"


@pytest.mark.skipif(
    not (FROZEN_DIR / "requirements.json").is_file(),
    reason="the frozen TPS54332 spec is a local, untracked input (models/T1-tps54332/spec)",
)
def test_the_frozen_tps54332_spec_is_represented_and_independently_tested() -> None:
    spec = load_tps54320_spec(
        FROZEN_DIR / "requirements.json",
        FROZEN_DIR / "bindings.json",
        part="TPS54332DDA",
        subckt="TPS54332DDA",
    )
    decision = decide_support("TPS54332DDA", title="TPS54332 3.5-A Step-Down Converter", spec=spec)
    assert decision.state == "supported", decision.missing
    assert decision.implementation == "peak_current_buck"


# --------------------------------------------------------------------------- #
# Reading what kind of part it is: number and title, first page, cited rows.


@pytest.mark.parametrize(
    ("part", "title", "head", "statements", "family", "source"),
    [
        ("LM358", "Dual Operational Amplifier", "", (), "amplifier_comparator", "title"),
        (
            "LM358",
            "lm358_datasheet",
            "LM358 Industry-Standard Dual Operational Amplifiers Features",
            (),
            "amplifier_comparator",
            "first page",
        ),
        (
            "LM358",
            "lm358_datasheet",
            "",
            ("Input offset voltage magnitude", "Typical gain bandwidth product"),
            "amplifier_comparator",
            "cited rows",
        ),
        ("XYZ1", "3.5-A Step-Down Converter", "", (), "switching_regulator", "title"),
        (
            "XYZ1",
            "xyz1_datasheet",
            "Low dropout linear regulator; power-good output",
            (),
            "linear_regulator",
            "first page",
        ),
    ],
)
def test_the_family_is_read_from_the_title_the_first_page_or_two_row_signals(
    part: str, title: str, head: str, statements: tuple[str, ...], family: str, source: str
) -> None:
    found = identify_family_scored(part=part, title=title, head=head, statements=statements)
    assert found is not None
    assert found[0] == family
    assert source in found[2]


@pytest.mark.parametrize(
    "statements",
    [
        (),
        ("Input offset voltage magnitude",),  # one signal proves nothing
        ("Use a 10 kohm resistor and a 1 uF capacitor with the inductor",),  # generic words
        ("A voltage", "The device latches", "There is a timer"),
    ],
)
def test_weak_or_generic_row_wording_identifies_nothing(statements: tuple[str, ...]) -> None:
    assert identify_family_scored(part="XYZ1", title="xyz1", statements=statements) is None


def test_the_title_outweighs_a_stray_word_on_the_first_page() -> None:
    found = identify_family_scored(
        part="XYZ1",
        title="Step-Down Converter",
        head="an optional watchdog and a power-good flag",
    )
    assert found is not None and found[0] == "switching_regulator"


def test_the_op_amp_rows_are_a_supported_dual_op_amp_read_from_the_rows_alone() -> None:
    spec = synthetic_dual_op_amp_spec()
    decision = decide_support(spec.part, title="lm358_datasheet", spec=spec)
    assert decision.state == "supported"
    assert decision.implementation == "dual_op_amp"
    assert decision.family == "amplifier_comparator"
    assert decision.missing == ()
    assert all(decision.allows(route) for route in ROUTES)


def test_an_amplifier_with_no_matching_pinout_is_identified_but_not_supported() -> None:
    spec = synthetic_dual_op_amp_spec()
    single = dataclasses.replace(spec, pin_map=spec.pin_map[:5])
    decision = decide_support(spec.part, title="lm358_datasheet", spec=single)
    assert decision.state == "unsupported_family"
    assert decision.family == "amplifier_comparator"
    assert "cited rows" in decision.identified_from
    assert decision.allows("legacy_ai") and not decision.allows("behavioral")


def test_a_declared_family_names_an_unidentified_part_but_never_supports_it() -> None:
    decision = decide_support("XYZ123", declared_family="linear_regulator")
    assert decision.state == "unsupported_family"
    assert decision.family == "linear_regulator"
    assert "declared by the operator" in decision.identified_from
    assert decision.allows("legacy_ai") and not decision.allows("behavioral")


def test_a_declared_family_cannot_unblock_a_class() -> None:
    decision = decide_support("STM32F407VGT6", declared_family="linear_regulator")
    assert decision.state == "blocked_class"
    assert not any(decision.allows(route) for route in ROUTES)


def test_a_declared_family_that_disagrees_with_the_evidence_says_so() -> None:
    decision = decide_support(
        "LM358", title="Dual Operational Amplifier", declared_family="passive"
    )
    assert decision.family == "passive"
    assert "operator" in decision.identified_from
    assert "amplifier" in decision.identified_from


def test_only_a_written_family_can_be_declared() -> None:
    with pytest.raises(ValueError, match="family must be one of"):
        decide_support("XYZ123", declared_family="quantum_widget")
    assert set(family_ids()) == {family_id for family_id, _label, _phrases in ORDINARY_FAMILIES}
