"""Template-family regression tests (Phase 6 step 1).

For each family contract in ``fixtures/templates/``:

* the contract loads and restates the family's subcircuit, port order and
  parameters exactly;
* the rendered ``.subckt`` line agrees with the contract (port order and every
  declared default) — the drift guard between data and the emitted text;
* the capability record carries exactly ``BEHAVIOR_KEYS``, with
  ``thermal_dependence`` never ``supported``;
* one real LTspice run reproduces the committed baseline within the declared
  tolerance.

Negative cases assert the honesty rules: a corrupted baseline value is FAIL,
and a value missing on either side is UNKNOWN — never PASS.

Baselines are regenerated (never hand-edited) with::

    uv run python tests/regression/test_templates.py --generate
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pytest

from boardmodeler.domain.enums import EvidenceLevel, ModelKind, Status
from boardmodeler.domain.hashing import sha256_text
from boardmodeler.domain.records import BEHAVIOR_KEYS
from boardmodeler.models.regression import (
    BaselineDocument,
    BaselineTolerance,
    compare_baseline,
    describe_baseline,
    load_baseline,
    run_family_deck,
    write_baseline,
)
from boardmodeler.models.regulator import regulator_text
from boardmodeler.models.templates import (
    FAMILIES,
    FAMILY_TEMPLATES,
    ContractError,
    TemplateContract,
    capability_for,
    instance_card,
    load_contract,
    parse_subckt_header,
    render_subckt,
    validate_rendered_subckt,
    write_application_deck,
)
from boardmodeler.simulation.ltspice import discover, locate

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "fixtures" / "templates"
BASELINE_DIR = Path(__file__).resolve().parent / "baselines"


def _contract(family: str) -> TemplateContract:
    return load_contract(TEMPLATES_DIR / f"{family}.json")


def _variant(tmp_path: Path, family: str, mutate) -> Path:
    """Write a deliberately broken copy of a contract and return its path."""
    data = json.loads((TEMPLATES_DIR / f"{family}.json").read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / f"{family}_variant.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# contract and rendered text


@pytest.mark.parametrize("family", FAMILIES)
def test_contract_restates_the_family_interface(family: str) -> None:
    contract = _contract(family)
    template = FAMILY_TEMPLATES[family]
    assert contract.family == family
    assert contract.subckt == template.subckt
    assert tuple(contract.ports) == template.ports
    declared = {parameter.name for parameter in contract.parameters}
    assert set(template.names) <= declared
    assert {name for name in declared if name not in template.names} == set()


@pytest.mark.parametrize("family", FAMILIES)
def test_rendered_subckt_matches_the_contract(family: str) -> None:
    contract = _contract(family)
    text = render_subckt(contract)
    header = parse_subckt_header(text, contract.subckt)
    assert header is not None, f"{family}: no .subckt {contract.subckt} line"
    assert header.ports == tuple(contract.ports)
    assert set(header.params) == set(FAMILY_TEMPLATES[family].names)
    for parameter in contract.parameters:
        assert header.params[parameter.name], f"{family}: {parameter.name} has no value"
    validate_rendered_subckt(contract, text)  # no-op when consistent


@pytest.mark.parametrize("family", FAMILIES)
def test_capability_covers_exactly_the_behavior_keys(family: str) -> None:
    contract = _contract(family)
    capability = capability_for(contract)
    assert set(capability.behaviors) == set(BEHAVIOR_KEYS)
    assert tuple(capability.behaviors) == BEHAVIOR_KEYS
    assert capability.kind is ModelKind.REDUCED_BEHAVIORAL
    assert capability.evidence_level is EvidenceLevel.SYNTHETIC_ANALYTICAL
    assert capability.source_model_hash == sha256_text(render_subckt(contract))
    assert capability.model_id == contract.part
    assert capability.behaviors["thermal_dependence"] == "unsupported"
    allowed = FAMILY_TEMPLATES[family].supported_keys
    for key, state in capability.behaviors.items():
        assert state in {"supported", "unsupported", "not_tested"}
        if state == "supported":
            assert key in allowed, f"{family}: {key} declared supported but not implemented"


@pytest.mark.parametrize("family", FAMILIES)
def test_deck_writes_the_library_and_instantiates_the_contract(family: str, tmp_path: Path) -> None:
    contract = _contract(family)
    application = write_application_deck(contract, tmp_path / "run" / "regression.cir")
    assert application.deck.is_file() and application.library.is_file()
    deck_text = application.deck.read_text(encoding="utf-8")
    library_text = application.library.read_text(encoding="utf-8")
    assert str(application.library.resolve()) in deck_text
    assert f" {contract.subckt}" in deck_text
    assert f".subckt {contract.subckt} " in library_text
    assert len(contract.deck.measures) >= 3


@pytest.mark.parametrize("family", ["buck", "ldo"])
def test_regulator_families_delegate_to_the_regulator_generator(family: str) -> None:
    contract = _contract(family)
    assert render_subckt(contract) == regulator_text(
        contract.subckt, extra_params=contract.parameter_values()
    )


@pytest.mark.parametrize("family", ["supervisor", "load_switch"])
def test_sensor_families_emit_their_primitives(family: str) -> None:
    text = render_subckt(_contract(family))
    assert ".subckt BM_SCHMITT " in text
    assert ".subckt BM_DELAY " in text


def test_a_new_part_in_a_family_is_pure_data(tmp_path: Path) -> None:
    data = json.loads((TEMPLATES_DIR / "supervisor.json").read_text(encoding="utf-8"))
    data["part"] = "supervisor_2v5"
    data["notes"] = "a new part declared only by this JSON file"
    for parameter in data["parameters"]:
        if parameter["name"] == "VTH_RISE":
            parameter["default"] = 2.5
        if parameter["name"] == "VTH_FALL":
            parameter["default"] = 2.4
    path = tmp_path / "supervisor_2v5.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    contract = load_contract(path)
    text = render_subckt(contract)
    assert ".subckt BM_SUPERVISOR VIN PG GND params: VTH_RISE=2.5 VTH_FALL=2.4" in text
    capability = capability_for(contract)
    assert capability.model_id == "supervisor_2v5"
    assert capability.behaviors["thermal_dependence"] == "unsupported"


def test_instance_card_requires_every_port() -> None:
    contract = _contract("supervisor")
    card = instance_card(contract, "X9", {"VIN": "vin", "PG": "pg", "GND": "0"}, {"PG_DELAY": 2e-3})
    assert card.startswith("X9 vin pg 0 BM_SUPERVISOR")
    assert "PG_DELAY=2m" in card
    with pytest.raises(ContractError, match="GND"):
        instance_card(contract, "X9", {"VIN": "vin", "PG": "pg"})
    with pytest.raises(ContractError, match="NOPE"):
        instance_card(contract, "X9", {"VIN": "vin", "PG": "pg", "GND": "0"}, {"NOPE": 1.0})


# --------------------------------------------------------------------------- #
# contract validation negatives


def test_unknown_parameter_is_rejected(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "supervisor",
        lambda data: data["parameters"].append(
            {"name": "NOPE", "type": "float", "unit": "V", "default": 1.0}
        ),
    )
    with pytest.raises(ContractError, match="NOPE"):
        load_contract(path)


def test_wrong_port_count_is_rejected(tmp_path: Path) -> None:
    path = _variant(tmp_path, "supervisor", lambda data: data.update(ports=["VIN", "PG"]))
    with pytest.raises(ContractError, match="ports"):
        load_contract(path)


def test_port_order_mismatch_is_rejected(tmp_path: Path) -> None:
    path = _variant(tmp_path, "supervisor", lambda data: data.update(ports=["VIN", "GND", "PG"]))
    with pytest.raises(ContractError, match="ports"):
        load_contract(path)


def test_missing_required_parameter_is_rejected(tmp_path: Path) -> None:
    def drop(data: dict) -> None:
        data["parameters"] = [p for p in data["parameters"] if p["name"] != "VTH_RISE"]

    with pytest.raises(ContractError, match="VTH_RISE"):
        load_contract(_variant(tmp_path, "supervisor", drop))


def test_behaviour_without_capability_key_is_rejected(tmp_path: Path) -> None:
    def drop(data: dict) -> None:
        del data["behaviors"][0]["capability"]

    with pytest.raises(ContractError, match="capability"):
        load_contract(_variant(tmp_path, "supervisor", drop))


def test_supported_claim_for_an_unimplemented_capability_is_rejected(tmp_path: Path) -> None:
    def claim_switching(data: dict) -> None:
        data["behaviors"].append(
            {
                "behavior": "switch_node",
                "capability": "switching_waveforms",
                "status": "supported",
                "detail": "",
            }
        )

    with pytest.raises(ContractError, match="switching_waveforms"):
        load_contract(_variant(tmp_path, "supervisor", claim_switching))


def test_loop_gain_claim_is_rejected_for_a_generated_regulator(tmp_path: Path) -> None:
    def claim_loop(data: dict) -> None:
        for claim in data["behaviors"]:
            if claim["capability"] == "compensation_loop":
                claim["status"] = "supported"

    with pytest.raises(ContractError, match="compensation_loop"):
        load_contract(_variant(tmp_path, "ldo", claim_loop))


def test_rendered_text_is_checked_against_the_contract() -> None:
    contract = _contract("supervisor")
    text = render_subckt(contract)
    swapped = text.replace(".subckt BM_SUPERVISOR VIN PG GND", ".subckt BM_SUPERVISOR PG VIN GND")
    assert swapped != text
    with pytest.raises(ContractError, match="ports"):
        validate_rendered_subckt(contract, swapped)
    extra = text.replace("params: VTH_RISE=2.9", "params: NOPE=1 VTH_RISE=2.9")
    assert extra != text
    with pytest.raises(ContractError, match="NOPE"):
        validate_rendered_subckt(contract, extra)
    with pytest.raises(ContractError, match="subckt"):
        validate_rendered_subckt(contract, "* nothing here\n")


# --------------------------------------------------------------------------- #
# baseline comparison (no simulator)


def test_corrupted_baseline_value_is_a_deviation() -> None:
    baseline = load_baseline(BASELINE_DIR / "ldo.json")
    name = next(iter(baseline.values))
    produced = dict(baseline.values)
    produced[name] = produced[name] * 1.25
    comparison = compare_baseline(produced, baseline, family="ldo")
    assert comparison.status is Status.FAIL
    assert any(finding.code == "baseline_deviation" for finding in comparison.findings)
    assert name in comparison.deviations


def test_deleting_a_baseline_key_yields_unknown_not_pass() -> None:
    baseline = load_baseline(BASELINE_DIR / "ldo.json")
    name = next(iter(baseline.values))
    shortened = baseline.model_copy(
        update={
            "values": {key: value for key, value in baseline.values.items() if key != name},
            "tolerances": {key: value for key, value in baseline.tolerances.items() if key != name},
        }
    )
    comparison = compare_baseline(dict(baseline.values), shortened)
    assert comparison.status is Status.UNKNOWN
    assert [finding.code for finding in comparison.findings] == ["produced_value_unbaselined"]
    assert name not in comparison.deviations


def test_missing_values_are_unknown_never_pass() -> None:
    baseline = BaselineDocument(
        family="supervisor",
        values={"a": 1.0, "b": 2.0},
        produced_by="test",
        note="literal baseline for the comparison rules",
    )
    missing_produced = compare_baseline({"a": 1.0}, baseline)
    assert missing_produced.status is Status.UNKNOWN
    assert [finding.code for finding in missing_produced.findings] == ["baseline_value_missing"]

    missing_baseline = compare_baseline({"a": 1.0, "b": 2.0, "c": 3.0}, baseline)
    assert missing_baseline.status is Status.UNKNOWN
    assert [finding.code for finding in missing_baseline.findings] == ["produced_value_unbaselined"]

    deviating = compare_baseline({"a": 1.0, "b": 2.0, "c": 3.0}, baseline)
    assert deviating.status is not Status.PASS


def test_default_tolerance_is_one_percent() -> None:
    baseline = BaselineDocument(
        family="supervisor",
        values={"x": 100.0, "zero": 0.0},
        tolerances={"zero": BaselineTolerance(rel=0.01, abs=0.05)},
        produced_by="test",
        note="literal baseline for the tolerance rules",
    )
    inside = compare_baseline({"x": 100.9, "zero": 0.04}, baseline)
    assert inside.status is Status.PASS, inside.summary()
    outside = compare_baseline({"x": 101.5, "zero": 0.04}, baseline)
    assert outside.status is Status.FAIL
    assert outside.deviations["x"] == pytest.approx(0.015)


def test_family_mismatch_is_refused() -> None:
    baseline = load_baseline(BASELINE_DIR / "buck.json")
    with pytest.raises(ValueError, match="family"):
        compare_baseline(dict(baseline.values), baseline, family="ldo")


def test_every_family_has_a_filled_baseline() -> None:
    for family in FAMILIES:
        baseline = load_baseline(BASELINE_DIR / f"{family}.json")
        contract = _contract(family)
        assert baseline.family == family
        assert set(baseline.values) == {measure.name for measure in contract.deck.measures}
        assert baseline.produced_by and baseline.note
        assert set(baseline.tolerances) <= set(baseline.values)


# --------------------------------------------------------------------------- #
# real runs


def test_baseline_document_round_trips(tmp_path: Path) -> None:
    original = load_baseline(BASELINE_DIR / "supervisor.json")
    path = write_baseline(tmp_path / "copy.json", original)
    assert load_baseline(path) == original


@pytest.mark.ltspice
@pytest.mark.parametrize("family", FAMILIES)
def test_baseline_is_reproduced_by_a_real_run(
    family: str, tmp_path: Path, ltspice_exe: Path
) -> None:
    contract = _contract(family)
    run = run_family_deck(contract, exe=ltspice_exe, workdir=tmp_path / family)
    baseline = load_baseline(BASELINE_DIR / f"{family}.json")
    assert set(run.values) == set(baseline.values)
    comparison = compare_baseline(run.values, baseline, family=family)
    assert comparison.status is Status.PASS, (
        f"{family}: {comparison.summary()}\nproduced={run.values}\nbaseline={baseline.values}"
    )
    # the generation path used to commit the baseline reproduces it exactly
    document = describe_baseline(contract, run)
    assert document.values == baseline.values
    assert document.family == family
    assert contract.deck.title in document.note
    assert "measure_raw" in document.note
    assert "run_family_deck" in document.produced_by


# --------------------------------------------------------------------------- #
# baseline generation (run explicitly; never in a normal test session)


def generate_baseline(family: str, workdir: Path) -> Path:
    """Run one family's deck and write the observed values as its baseline."""
    contract = load_contract(TEMPLATES_DIR / f"{family}.json")
    # Regenerating a baseline is an explicit ask (``--generate``), not a startup
    # probe: a configured path wins, and only then is the well-known locations
    # search used, exactly like the ``ltspice_install`` test fixture.
    install = locate() or discover().install
    if install is None:
        raise SystemExit(
            "no LTspice executable was configured or found, so no baseline can be "
            "produced; set LTSPICE_EXE to an LTspice.exe and re-run"
        )
    run = run_family_deck(contract, exe=install.path, workdir=workdir / family)
    document = describe_baseline(contract, run)
    return write_baseline(BASELINE_DIR / f"{family}.json", document)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="regenerate template baselines")
    parser.add_argument("--generate", action="store_true", required=True)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("families", nargs="*", default=None)
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="bm-baselines-"))
    for family in args.families or list(FAMILIES):
        path = generate_baseline(family, workdir)
        print(f"{family}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
