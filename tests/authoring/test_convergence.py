"""The convergence lint and log diagnosis, on the structures that sank a real build.

Every model below is reduced from the TPS54332DDA candidates of 2026-09-24: turn 1
wrote ``en_ok`` bare inside expressions (LTspice: "No such parameter defined") and
turn 2 held its PWM latch on a node whose only path to ground was 1 TΩ (every
operating-point method failed, "trouble with node en").
"""

from __future__ import annotations

from pathlib import Path

from boardmodeler.authoring.convergence import (
    diagnose_log,
    fix_bare_nodes,
    floating_permitted,
    lint_library,
    read_log,
)

BARE = """\
.subckt DUT BOOT VIN EN SS GND
Ben_ok en_ok GND V = limit((V(EN,GND)-1.2)/0.1, 0, 1)
Ren_ok en_ok GND 1Meg
Bss VIN SS I = en_ok * 2u
Rss SS GND 100Meg
Rpu VIN EN 10Meg
.ends DUT
"""

LATCH = """\
.subckt DUT VIN EN GND
Rpu VIN EN 10Meg
Csw sw GND 1n
Rsw sw GND 1T
Bsw_tgt sw_tgt GND V = if(V(EN,GND)>1.25, 1, V(sw,GND))
Bsw GND sw I = (V(sw_tgt,GND) - V(sw,GND))*1n/1n
Bself loop GND V = V(loop,GND)*0.5 + 1
.ends DUT
"""

FLOATING_EN = """\
.subckt DUT VIN EN GND
Ben VIN EN I = 1u
Cen EN GND 1p
.ends DUT
"""


def codes(text: str, **kwargs) -> set[str]:
    return {finding.code for finding in lint_library(text, **kwargs)}


def test_bare_node_name_in_an_expression_is_an_error_naming_the_fix() -> None:
    findings = [f for f in lint_library(BARE) if f.code == "bare_node_in_expression"]
    assert [f.element for f in findings] == ["Bss"]
    assert "V(en_ok,GND)" in findings[0].message


def test_bare_node_fix_rewrites_only_the_reference_and_clears_the_finding() -> None:
    fixed, changes = fix_bare_nodes(BARE)
    assert changes == ["line 4: en_ok -> V(en_ok,GND)"]
    assert "Bss VIN SS I = V(en_ok,GND) * 2u" in fixed
    assert fixed.replace("V(en_ok,GND) * 2u", "en_ok * 2u") == BARE
    assert "bare_node_in_expression" not in codes(fixed)


def test_parameters_functions_and_numbers_are_not_mistaken_for_nodes() -> None:
    text = BARE.replace("en_ok * 2u", "Iss * limit(V(SS,GND), 0, 1) * 1e-6")
    text = text.replace(".subckt DUT BOOT VIN EN SS GND", ".subckt DUT BOOT VIN EN SS GND")
    text = text.replace("Ren_ok", ".param Iss=2u\nRen_ok")
    assert fix_bare_nodes(text)[1] == []
    assert not codes(text) & {"bare_node_in_expression", "undefined_identifier"}


def test_state_held_by_a_terabyte_resistor_and_a_self_read_are_errors() -> None:
    found = codes(LATCH)
    assert "no_dc_path" in found
    assert "resistor_not_a_dc_path" in found
    assert "self_referencing_source" in found


def test_a_floating_permitted_pin_must_be_biased_by_the_model_itself() -> None:
    assert "no_dc_path" not in codes(FLOATING_EN)  # an ordinary port is the fixture's job
    assert "no_dc_path" in codes(FLOATING_EN, floating_ok=("EN",))
    biased = FLOATING_EN.replace("Cen EN GND 1p", "Cen EN GND 1p\nRen EN GND 100Meg")
    assert "no_dc_path" not in codes(biased, floating_ok=("EN",))


def test_float_permission_comes_from_the_extracted_pin_table() -> None:
    pins = [
        {"name": "EN", "function": "Enable pin. Float to enable."},
        {"name": "SS", "function": "Slow-start pin", "behavior": ["capacitor sets ramp"]},
    ]
    assert floating_permitted(pins) == ("EN",)


def test_log_diagnosis_names_the_rejected_line_and_the_nonconvergent_node(tmp_path) -> None:
    log = tmp_path / "deck.log"
    log.write_bytes(
        (
            "LTspice 26.0.0 for Windows\n"
            "C:\\models\\DUT.lib(4): No such parameter defined.\n"
            "Bss VIN SS I = en_ok * 2u\n"
            "               ^^^^^\n"
            "Direct Newton iteration failed to find operating point.\n"
            "Gmin stepping failed to find operating point.\n"
            'Convergence Failure:  Time step too small; initial timepoint: trouble with node "en"\n'
        ).encode("utf-16-le")
    )
    hints = diagnose_log(read_log(Path(log)), BARE)
    assert any("line 4" in h and "en_ok" in h and "V(node,GND)" in h for h in hints)
    assert any("node `en`" in h and "Rpu VIN EN 10Meg" in h for h in hints)
    assert any("No DC operating point" in h for h in hints)
