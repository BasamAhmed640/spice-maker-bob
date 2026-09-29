"""The viability gate passes a model that sits correctly in a circuit and fails each way a
model can sit wrongly.

``OPX`` is a hand-built reference: an op amp whose supply pins carry its quiescent and
output current, whose output is current limited, whose inputs are clamped, with a separate
exposed pad and a no-connect pin. Every mutant below breaks exactly one thing about how
the model sits in a circuit and must be caught by the check that names it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardmodeler.authoring.viability import GatePin, GateSpec, run_gate, static_checks
from boardmodeler.domain.enums import Status
from boardmodeler.models.symbolism import symbol_text

REPO = Path(__file__).resolve().parents[2]
LM358_EVIDENCE = REPO / "fixtures/models/lm358_committed_2026-09-24.lib"

OPX = """\
.subckt OPX VCC OUT INM INP VEE PAD NCX
* Quiescent supply current: 500 uA at 5 V, nothing when unpowered
Bq VCC VEE I=100u*limit(V(VCC,VEE),0,5)
* High-impedance inputs, clamped to both rails
Rinp INP VEE 1G
Rinm INM VEE 1G
Dc1 INP VCC Dcl
Dc2 VEE INP Dcl
Dc3 INM VCC Dcl
Dc4 VEE INM Dcl
* Transconductance stage into a compensation node (DC gain 1000)
Bgm VEE n1 I=100u*tanh((V(INP,VEE)-V(INM,VEE))/1m)
R1 n1 VEE 10Meg
C1 n1 VEE 30p
Dc5 n1 VCC Dcl
Dc6 VEE n1 Dcl
* Desired output level, held 1.5 V inside the supply
Bdrv drv VEE V=limit(V(n1,VEE),0.05,max(V(VCC,VEE)-1.5,0.05))
* Push-pull output: 50 ohm, 40 mA; sourced from VCC, sunk to VEE
Bs VCC OUT I=limit((V(drv,VEE)-V(OUT,VEE))/50,0,40m)
Bk OUT VEE I=limit((V(OUT,VEE)-V(drv,VEE))/50,0,40m)
Rout OUT VEE 1G
* Exposed pad: its own pin, convergence leak only
Rpad PAD VEE 1G
.model Dcl D(Is=1e-14)
.ends OPX
"""

PORTS = ("VCC", "OUT", "INM", "INP", "VEE", "PAD", "NCX")
SPEC = GateSpec(
    "OPX",
    "OPX",
    (
        GatePin("VCC", "supply", "1", vtest=5.0),
        GatePin("OUT", "output", "2", isc_max=0.060),
        GatePin("INM", "input", "3"),
        GatePin("INP", "input", "4"),
        GatePin("VEE", "ground", "5"),
        GatePin("PAD", "pad", "6"),
        GatePin("NCX", "nc", "7"),
    ),
)
ASY = symbol_text("OPX", PORTS, model_file="OPX.lib")


def broken(old: str, new: str) -> str:
    assert old in OPX, old
    return OPX.replace(old, new)


def status_of(checks, check_id: str) -> Status:
    return next(c.status for c in checks if c.id == check_id)


# --------------------------------------------------------------------------- #
# static: the reference is clean, and each structural mistake is named


def test_the_reference_model_is_clean_statically() -> None:
    checks = static_checks(OPX, SPEC, asy_text=ASY)
    assert {c.id for c in checks} == {
        "ports_match_pin_table",
        "symbol_order_matches_ports",
        "no_global_ground",
        "no_internal_pin_ties",
        "dc_paths",
    }
    assert [c.status for c in checks] == [Status.PASS] * 5, [c.detail for c in checks]


STATIC_MUTANTS = {
    "a pin missing from the subcircuit": (
        broken(".subckt OPX VCC OUT INM INP VEE PAD NCX", ".subckt OPX VCC OUT INM INP VEE PAD"),
        "ports_match_pin_table",
    ),
    "a port the pin table does not list": (
        broken(
            ".subckt OPX VCC OUT INM INP VEE PAD NCX",
            ".subckt OPX VCC OUT INM INP VEE PAD NCX EXTRA",
        ),
        "ports_match_pin_table",
    ),
    "ports swapped against the symbol": (
        broken(
            ".subckt OPX VCC OUT INM INP VEE PAD NCX", ".subckt OPX VCC OUT INP INM VEE PAD NCX"
        ),
        "symbol_order_matches_ports",
    ),
    "the exposed pad tied to ground inside the model": (
        broken("Rpad PAD VEE 1G", "Rpad PAD VEE 1m"),
        "no_internal_pin_ties",
    ),
    "a source that returns through node 0": (
        broken("Bk OUT VEE I=", "Bk OUT 0 I="),
        "no_global_ground",
    ),
    "an expression that reads the global GND": (
        broken("V(INP,VEE)-V(INM,VEE)", "V(INP,GND)-V(INM,GND)"),
        "no_global_ground",
    ),
    "a source that reads its own output": (
        broken("Rpad PAD VEE 1G", "Rpad PAD VEE 1G\nBself xf VEE V=V(xf,VEE)+1"),
        "dc_paths",
    ),
}


@pytest.mark.parametrize("name", list(STATIC_MUTANTS))
def test_each_structural_mistake_is_named(name: str) -> None:
    text, expected = STATIC_MUTANTS[name]
    checks = static_checks(text, SPEC, asy_text=ASY)
    assert status_of(checks, expected) is Status.FAIL, [c.as_dict() for c in checks]


def test_a_cited_tie_is_allowed() -> None:
    text = broken("Rpad PAD VEE 1G", "Rpad PAD VEE 1m")
    spec = GateSpec(SPEC.part, SPEC.subckt, SPEC.pins, allowed_ties=(("PAD", "VEE"),))
    assert status_of(static_checks(text, spec, asy_text=ASY), "no_internal_pin_ties") is Status.PASS


def test_a_subcircuit_that_is_not_there_fails_instead_of_raising() -> None:
    checks = static_checks("* nothing here\n", SPEC)
    assert checks[0].status is Status.FAIL


def test_a_static_only_report_is_never_pass(tmp_path: Path) -> None:
    lib = tmp_path / "OPX.lib"
    lib.write_text(OPX, encoding="utf-8")
    report = run_gate(lib, SPEC, Path("does-not-exist.exe"), tmp_path, dynamic=False)
    assert report.status is Status.UNKNOWN
    report = run_gate(lib, SPEC, Path("does-not-exist.exe"), tmp_path)
    assert report.status is Status.UNKNOWN
    assert [c.status for c in report.checks if c.id == "dynamic_benches"] == [Status.BLOCKED]


# --------------------------------------------------------------------------- #
# dynamic: real LTspice runs


@pytest.mark.ltspice
def test_the_reference_model_passes_every_bench(ltspice_exe: Path, tmp_path: Path) -> None:
    lib = tmp_path / "OPX.lib"
    lib.write_text(OPX, encoding="utf-8")
    report = run_gate(lib, SPEC, ltspice_exe, tmp_path / "gate")
    assert report.status is Status.PASS, [c.as_dict() for c in report.checks]
    assert report.runs == 15
    by_id = {c.id: c for c in report.checks}
    # the outputs really delivered current into shorts, so the supply check was exercised
    assert by_id["supply_carries_output_current"].status is Status.PASS
    assert by_id["output_short_limited"].measured["peak_a_OUT"] == pytest.approx(0.04, rel=0.05)


DYNAMIC_MUTANTS = {
    "an output with no current limit and no output resistance": (
        OPX.replace("/50,0,40m)", "/0.01,0,1e6)"),
        "output_short_limited",
    ),
    "output current sourced from ground, not from VCC": (
        broken("Bs VCC OUT I=", "Bs VEE OUT I="),
        "supply_carries_output_current",
    ),
    "output current returned through node 0": (
        broken("Bk OUT VEE I=", "Bk OUT 0 I="),
        "current_conservation",
    ),
    "a floating input that rises far above the rail": (
        broken("Dc1 INP VCC Dcl", "Bib INP VEE I=-20n"),
        "floating_inputs_in_rails",
    ),
    "a supply pin that draws nothing": (
        broken("Bq VCC VEE I=100u*limit(V(VCC,VEE),0,5)", "* no quiescent current"),
        "supply_draws_current",
    ),
    "a no-connect pin that is not inert": (
        broken("Rpad PAD VEE 1G", "Rpad PAD VEE 1G\nRnc NCX VEE 1k"),
        "nc_pins_inert",
    ),
}


@pytest.mark.ltspice
@pytest.mark.parametrize("name", list(DYNAMIC_MUTANTS))
def test_each_way_of_sitting_wrongly_in_a_circuit_is_caught(
    name: str, ltspice_exe: Path, tmp_path: Path
) -> None:
    text, expected = DYNAMIC_MUTANTS[name]
    lib = tmp_path / "OPX.lib"
    lib.write_text(text, encoding="utf-8")
    report = run_gate(lib, SPEC, ltspice_exe, tmp_path / "gate")
    by_id = {c.id: c for c in report.checks}
    assert by_id[expected].status is Status.FAIL, [c.as_dict() for c in report.checks]
    assert report.status is Status.FAIL


LM358_SPEC = GateSpec(
    "LM358",
    "LM358",
    (
        GatePin("VCC", "supply", "8", vtest=5.0),
        GatePin(
            "OUT1", "output", "1", isc_max=0.060
        ),  # datasheet: short-circuit +-40 typ, +-60 mA max
        GatePin("IN1M", "input", "2"),
        GatePin("IN1P", "input", "3"),
        GatePin("VEE", "ground", "4"),
        GatePin("IN2P", "input", "5"),
        GatePin("IN2M", "input", "6"),
        GatePin("OUT2", "output", "7", isc_max=0.060),
    ),
)


@pytest.mark.ltspice
def test_the_committed_lm358_model_is_not_viable_in_a_system(
    ltspice_exe: Path, tmp_path: Path
) -> None:
    """The repo's committed LM358 passes 32 datasheet rows and still fails as a system part."""
    report = run_gate(LM358_EVIDENCE, LM358_SPEC, ltspice_exe, tmp_path / "gate")
    failing = {c.id for c in report.failing()}
    assert {
        "no_global_ground",
        "current_conservation",
        "output_short_limited",
        "supply_carries_output_current",
        "floating_inputs_in_rails",
    } <= failing
    assert report.status is Status.FAIL


def test_a_switching_part_gets_a_finer_solver_step_and_a_longer_bench() -> None:
    from boardmodeler.authoring.viability import GateSpec as Spec
    from boardmodeler.authoring.viability import _tran

    plain = _tran(SPEC).card()
    fine = _tran(Spec("X", "X", SPEC.pins, tstop=1.5e-3, tmax=20e-9)).card()
    assert plain.startswith(".tran ") and plain.count(" ") == 2  # tstep tstop only
    assert fine.split()[2:] == ["0.0015", "0", "2e-08"]
