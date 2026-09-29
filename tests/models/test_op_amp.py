"""The dual op amp implementation: a typed design from cited rows, a pure renderer, exact provenance.

The offline tests run on a synthetic spec (the frozen LM358 rows are local and git-ignored); one
test compares the design read from the real frozen rows with it when they are present.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from tests.models.op_amp_spec import synthetic_dual_op_amp_spec

from boardmodeler.authoring.spec import SpecSet
from boardmodeler.authoring.viability import GatePin, GateSpec, run_gate
from boardmodeler.domain.enums import Status
from boardmodeler.models import op_amp
from boardmodeler.models.library import subckt_ports
from boardmodeler.models.support import decide_support
from boardmodeler.models.symbolism import symbol_text

FROZEN_LM358 = Path(__file__).resolve().parents[2] / "models" / "L1-lm358" / "spec"


@pytest.fixture(scope="module")
def spec() -> SpecSet:
    return synthetic_dual_op_amp_spec()


@pytest.fixture(scope="module")
def design(spec: SpecSet) -> op_amp.OpAmpDesign:
    found = op_amp.design_from_spec(spec)
    assert found is not None
    return found


def _by_name(design: op_amp.OpAmpDesign) -> dict[str, op_amp.ParameterOrigin]:
    return {item.name: item for item in design.parameters}


def test_every_essential_input_is_a_cited_typical_value(design: op_amp.OpAmpDesign) -> None:
    by_name = _by_name(design)
    cited = {name for name, item in by_name.items() if item.origin == "cited_row"}
    assert set(op_amp.ESSENTIAL_INPUTS) <= cited
    defaults = {name for name, item in by_name.items() if item.origin == "template_default"}
    assert defaults == {"ISC", "ROUT"}, "only the two non-essential output numbers are defaults"
    assert by_name["VOS"].value == pytest.approx(3e-3)
    assert by_name["IB"].value == pytest.approx(20e-9)
    assert by_name["GBW"].value == pytest.approx(0.7e6)
    assert by_name["SR"].value == pytest.approx(0.3e6)
    assert by_name["VOH_HEADROOM"].value == pytest.approx(2.0)
    assert all(
        item.row_id and item.page and item.excerpt
        for item in design.parameters
        if item.origin == "cited_row"
    )


def test_the_ports_keep_the_datasheet_pin_order_and_the_library_declares_them(
    design: op_amp.OpAmpDesign,
) -> None:
    assert design.ports == ("OUT1", "IN1M", "IN1P", "VEE", "IN2P", "IN2M", "OUT2", "VCC")
    assert subckt_ports(op_amp.render_library(design), design.subckt) == design.ports


def test_rendering_is_pure_and_writes_the_cited_numbers_into_the_core(
    design: op_amp.OpAmpDesign,
) -> None:
    text = op_amp.render_library(design)
    assert text == op_amp.render_library(design)
    # 3 mV offset, gain 1e5 through 1 mS (100 Meg), a 2 V high-output headroom, 5 mV low output
    for fragment in ("- 0.003", "R1 n1 VEE 100000000", "V(VCC,VEE)-2,0.005"):
        assert fragment in text
    assert "chk_ovl_OUT1" in text, "the shell writes the overload alarm for an output"


def test_the_record_ties_the_design_to_the_bytes_delivered(design: op_amp.OpAmpDesign) -> None:
    rendered = op_amp.render_library(design).encode("utf-8")
    assert design.record(rendered)["association"] == "exact"
    assert design.record(rendered + b"* edited\n")["association"] == "invalid_after_change"
    assert design.record(None)["association"] == "not_delivered"
    record = design.record(rendered)
    assert record["design_sha256"] == design.sha256
    assert record["verdict"] == "UNJUDGED"


def test_a_saved_design_reads_back_and_an_altered_one_is_refused(
    design: op_amp.OpAmpDesign,
) -> None:
    payload = json.loads(design.to_json())
    assert op_amp.OpAmpDesign.from_payload(payload) == design
    for edit in (
        lambda p: p["parameters"][3].update(value=-1.0),
        lambda p: p["parameters"][3].update(value=float("nan")),
        lambda p: p["parameters"][0].update(origin="template_default"),
        lambda p: p["parameters"][1].update(row_id=None),
        lambda p: p["pin_roles"].update(VCC="ground"),
        lambda p: p.update(spec_digest=None),
        lambda p: p.update(record_kind="buck_design"),
        lambda p: p.pop("part"),
    ):
        altered = json.loads(design.to_json())
        edit(altered)
        with pytest.raises(op_amp.OpAmpDesignError):
            op_amp.OpAmpDesign.from_payload(altered)


def _decide(spec: SpecSet, **changes):
    return decide_support(spec.part, title="", spec=dataclasses.replace(spec, **changes))


def test_a_supported_claim_needs_every_input_cited_and_every_behaviour_tested(
    spec: SpecSet,
) -> None:
    decision = decide_support(spec.part, title="", spec=spec)
    assert decision.state == "supported" and decision.implementation == "dual_op_amp"
    assert decision.missing == ()


def test_a_typical_value_that_is_not_cited_becomes_a_default_and_withdraws_the_claim(
    spec: SpecSet,
) -> None:
    stripped = tuple(
        dataclasses.replace(row, typ_value=None) if row.probe == "opamp_gbw" else row
        for row in spec.characteristics
    )
    decision = _decide(spec, characteristics=stripped)
    assert decision.state == "unsupported_family"
    assert "cited input GBW" in decision.missing
    assert not decision.allows("behavioral")


def test_a_behaviour_with_no_bound_row_withdraws_the_claim(spec: SpecSet) -> None:
    unbound = tuple(
        dataclasses.replace(row, probe=None, not_testable_reason="removed for the test")
        if row.probe in ("opamp_slew_rise", "opamp_slew_fall")
        else row
        for row in spec.characteristics
    )
    decision = _decide(spec, characteristics=unbound)
    assert decision.state == "unsupported_family"
    assert "independent test for slew rate" in decision.missing


def test_a_row_whose_citation_was_not_verified_is_never_a_cited_input(spec: SpecSet) -> None:
    ids = {row.char_id for row in spec.characteristics if row.probe == "opamp_gbw"}
    found = op_amp.design_from_spec(spec, unverified=ids)
    assert found is not None
    gbw = _by_name(found)["GBW"]
    assert gbw.origin == "template_default" and gbw.row_id is None
    assert decide_support(spec.part, title="", spec=spec, unverified=ids).state == (
        "unsupported_family"
    )


def test_only_the_eight_pin_dual_pinout_with_an_op_amp_row_is_recognised(spec: SpecSet) -> None:
    assert op_amp.design_from_spec(dataclasses.replace(spec, pin_map=spec.pin_map[:7])) is None
    assert op_amp.design_from_spec(dataclasses.replace(spec, pin_map=())) is None
    no_probe = tuple(
        dataclasses.replace(row, probe=None, not_testable_reason="removed")
        for row in spec.characteristics
    )
    assert op_amp.design_from_spec(dataclasses.replace(spec, characteristics=no_probe)) is None


def test_inconsistent_cited_values_are_refused_and_never_rendered(spec: SpecSet) -> None:
    edited = tuple(
        dataclasses.replace(row, typ_value=1e-6) if row.probe == "opamp_offset_current" else row
        for row in spec.characteristics
    )
    with pytest.raises(op_amp.OpAmpDesignError, match="offset current"):
        op_amp.design_from_spec(dataclasses.replace(spec, characteristics=edited))
    assert _decide(spec, characteristics=edited).state == "unsupported_family"


@pytest.mark.skipif(
    not (FROZEN_LM358 / "characteristics.json").is_file(),
    reason="needs the local frozen LM358 rows (models/L1-lm358/spec, git-ignored)",
)
def test_the_frozen_lm358_rows_give_the_same_design_values_as_the_stand_in(
    design: op_amp.OpAmpDesign,
) -> None:
    real = SpecSet.from_json((FROZEN_LM358 / "characteristics.json").read_text(encoding="utf-8"))
    found = op_amp.design_from_spec(real)
    assert found is not None
    assert {n: i.value for n, i in _by_name(found).items()} == pytest.approx(
        {n: i.value for n, i in _by_name(design).items()}
    )
    assert decide_support("LM358", title="lm358_datasheet", spec=real).state == "supported"


def _gate_for(design: op_amp.OpAmpDesign) -> GateSpec:
    """The gate claims only what the design has: no absolute-maximum row was cited, so no abs alarm."""
    roles = design.payload()["pin_roles"]
    pins = tuple(
        GatePin(
            port,
            roles[port],
            str(number),
            vtest=5.0 if roles[port] == "supply" else None,
            alarms=("ovl",) if roles[port] == "output" else (),
        )
        for number, port in enumerate(design.ports, start=1)
    )
    return GateSpec(design.part, design.subckt, pins)


@pytest.mark.ltspice
def test_the_rendered_op_amp_passes_the_viability_gate(
    design: op_amp.OpAmpDesign, tmp_path: Path, ltspice_exe: Path
) -> None:
    lib = tmp_path / f"{design.subckt}.lib"
    lib.write_text(op_amp.render_library(design), encoding="utf-8", newline="\n")
    asy = tmp_path / f"{design.subckt}.asy"
    asy.write_text(
        symbol_text(design.subckt, list(design.ports), model_file=lib.name), encoding="utf-8"
    )
    report = run_gate(lib, _gate_for(design), ltspice_exe, tmp_path / "gate", asy_path=asy)
    # nothing fails; the one check without a cited number to judge against says UNKNOWN,
    # never PASS (the rows carry no numeric short-circuit current)
    failing = [c.as_dict() for c in report.checks if c.status in (Status.FAIL, Status.BLOCKED)]
    assert not failing, failing
    unknown = [c.id for c in report.checks if c.status is Status.UNKNOWN]
    assert unknown == ["output_short_limited"]
    assert sum(c.status is Status.PASS for c in report.checks) == len(report.checks) - 1
