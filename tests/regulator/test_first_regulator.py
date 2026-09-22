"""First regulator against the frozen requirement set (Phase 2 step 8).

Two model families are involved, and the tests say which is which:

* the **generated type-B template** (`BM_REG_BUCK`) carries the verified
  startup / enable / power-good / current-limit behaviour. It is deterministic and
  fast, so it is what these tests exercise; its capability record is probed and
  exported like any other model.
* the **ported TI vendor model** (type A) is exercised by
  ``tests/regulator/test_vendor_*.py``, which is opt-in because that
  transistor-level model needs tens of seconds of wall time per simulated
  millisecond and can exceed any sane timeout on a longer window (observed: a
  2.1 ms application run hit the 600 s cap). Its capability record says exactly
  that — 4 behaviours probed as supported, the rest unknown/not_tested. See
  ``docs/DECISIONS.md`` D-010.

The decisive requirement in both cases is the same: **regulation must follow the
external feedback divider**, so a broken divider is detected instead of being
masked by a fixed internal output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import numpy as np
import pytest

from boardmodeler.domain.enums import EvidenceLevel, ModelKind, Status
from boardmodeler.domain.records import (
    BEHAVIOR_KEYS,
    ExpectationSpec,
    ModelCapability,
    Requirement,
    TestCase,
)
from boardmodeler.models.regulator import write_regulator_library
from boardmodeler.pipeline.runner import RunContext, run_case
from boardmodeler.simulation.deck import DeckSpec, Include, Source, TranSpec, write_deck
from boardmodeler.simulation.ltspice import LtspiceInstall
from boardmodeler.verification.engine import evaluate_case, gate_from_capability

pytestmark = pytest.mark.ltspice

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"
REQUIREMENTS_FILE = FIXTURE / "requirements.json"
CAPABILITY_FILE = FIXTURE / "capability_vendor.json"

VREF = 0.8
RFBB = 3.24e3
RFBT_NOMINAL = 10e3
TSTOP = 6e-3
TMAX = 2e-6
LOAD = 10.0
VOUT_NOMINAL = VREF * (1 + RFBT_NOMINAL / RFBB)

#: The template's own default retry interval (8 ms) outlasts this 6 ms window, so
#: the recoverability of a hiccup-limited output would not be observable. Declared
#: explicitly rather than assumed.
RETRY_S = 200e-6

BehaviorState = Literal["supported", "unsupported", "unknown", "not_tested"]


def load_requirements() -> dict[str, Requirement]:
    raw = json.loads(REQUIREMENTS_FILE.read_text(encoding="utf-8"))
    return {item["req_id"]: Requirement.model_validate(item) for item in raw["requirements"]}


def capability() -> ModelCapability | None:
    if not CAPABILITY_FILE.is_file():
        return None
    raw = json.loads(CAPABILITY_FILE.read_text(encoding="utf-8"))
    return ModelCapability.model_validate(raw["model"])


def requirement_with(
    req_id: str,
    expression: dict[str, object],
    *,
    statement: str | None = None,
) -> Requirement:
    """A frozen requirement with its expression replaced.

    Built through ``model_validate`` on a JSON round trip, never
    ``model_copy(update=...)``: an update would leave the new expression as a raw
    dict and every downstream evaluator would see a dict where it expects the
    constrained AST.
    """
    base = load_requirements()[req_id]
    payload = json.loads(base.model_dump_json())
    payload["expression"] = expression
    if statement is not None:
        payload["statement"] = statement
    return Requirement.model_validate(payload)


def regulation_requirement() -> Requirement:
    return requirement_with(
        "REQ_TPS54320_ELEC_020",
        {
            "op": "between",
            "signal": "V(VOUT)",
            "low": VOUT_NOMINAL * 0.97,
            "high": VOUT_NOMINAL * 1.03,
            "unit": "V",
            "interval": {"start_s": 0.8 * TSTOP, "end_s": TSTOP},
        },
        statement=("V(VOUT) regulates to Vref * (1 + Rfbt/Rfbb) through the external divider."),
    )


def case(name: str, requirement_ids: list[str], *, kind: str = "satisfy") -> TestCase:
    return TestCase(
        test_id=f"T_{name}",
        requirement_ids=requirement_ids,
        scenario_id="nominal_startup",
        scope="circuit_compliance",
        deck_template="app.cir",
        expected=ExpectationSpec(kind=kind, detail=f"{name} against the frozen requirement"),
        measurement=["V(VOUT)", "V(FB)", "V(PG)", "V(EN)"],
    )


def overload_deck(run_dir: Path, *, rfbt: float, params: dict[str, object] | None = None) -> Path:
    """A short application deck with a PWL current-source overload.

    The overload is a ramped current source rather than a switched resistor: an
    ideal switch is a hard discontinuity and forces the solver to nanosecond steps
    across the whole window (observed: a 6 ms window with a switch did not finish
    in 3.5 minutes of CPU), while a PWL ramp expresses the same "the load steps
    beyond the limit" intent and stays tractable.
    """
    overload_s = 1.2e-3
    clear_s = 2.2e-3
    stop = 3.5e-3
    library = run_dir.parent / "board.lib"
    if not library.is_file():
        write_regulator_library(library, ["BM_REG_BUCK"])
    overrides: dict[str, object] = {
        "VREF": VREF,
        "CSS": "1n",
        "ILIM": "1.0",
        "ILIM_MODE": "0",
        "RETRY_MS": RETRY_S,
    }
    overrides.update(params or {})
    params_text = " ".join(f"{key}={value}" for key, value in sorted(overrides.items()))
    edge = 10e-6
    deck = DeckSpec(
        title=f"generated buck with a {2.0:g} A overload from {overload_s * 1e3:g} ms",
        includes=(Include(path=str(library.resolve())),),
        sources=(
            Source.ramp("V1", "VIN", "0", v0=0.0, v1=12.0, rise_s=200e-6, hold_s=stop),
            Source.pulse(
                "Ven", "EN", "0", v1=0.0, v2=3.3, delay_s=0.3e-3, width_s=stop, period_s=2 * stop
            ),
            Source.dc("Vpg", "PGREF", "0", 3.3),
            Source(
                name="Iov",
                terminals=("VOUT", "0"),
                kind="pwl",
                points=(
                    (0.0, 0.0),
                    (overload_s - edge, 0.0),
                    (overload_s, 2.0),
                    (clear_s, 2.0),
                    (clear_s + edge, 0.0),
                    (stop + 1e-3, 0.0),
                ),
            ),
        ),
        elements=(
            f"XU1 VIN EN FB PG VOUT 0 SW 0 BM_REG_BUCK {params_text}",
            f"Rload VOUT 0 {LOAD:g}",
            "Rpg PG PGREF 10k",
            f"Rfbt VOUT FB {rfbt:g}",
            f"Rfbb FB 0 {RFBB:g}",
        ),
        tran=TranSpec(tstep=stop / 5000.0, tstop=stop, tstart=0.0, tmax=TSTOP / 2000.0),
        save=("V(VOUT)", "V(FB)", "V(PG)", "V(EN)"),
        options={"method": "gear", "trtol": 20},
    )
    return write_deck(deck, run_dir / "app.cir")


def buck_deck(
    run_dir: Path,
    *,
    rfbt: float,
    rfbb: float = RFBB,
    params: dict[str, object] | None = None,
) -> Path:
    """Application deck around the generated buck template.

    ``PG`` is open-drain in the model, so the deck must provide the pull-up the
    application circuit always has; without one, PG low is correct behaviour and
    not a model defect.
    """
    library = run_dir.parent / "board.lib"
    if not library.is_file():
        write_regulator_library(library, ["BM_REG_BUCK"])
    overrides: dict[str, object] = {
        "VREF": VREF,
        "CSS": "1n",
        "ILIM": "3.0",
        "ILIM_MODE": "0",
        "RETRY_MS": RETRY_S,
    }
    overrides.update(params or {})
    params_text = " ".join(f"{key}={value}" for key, value in sorted(overrides.items()))

    sources = [
        Source.ramp("V1", "VIN", "0", v0=0.0, v1=12.0, rise_s=200e-6, hold_s=TSTOP),
        Source.pulse(
            "Ven", "EN", "0", v1=0.0, v2=3.3, delay_s=0.3e-3, width_s=TSTOP, period_s=2 * TSTOP
        ),
        Source.dc("Vpg", "PGREF", "0", 3.3),
    ]
    elements = [
        f"XU1 VIN EN FB PG VOUT 0 SW 0 BM_REG_BUCK {params_text}",
        f"Rload VOUT 0 {LOAD:g}",
        "Rpg PG PGREF 10k",
        f"Rfbt VOUT FB {rfbt:g}",
        f"Rfbb FB 0 {rfbb:g}",
    ]
    deck = DeckSpec(
        title=f"generated buck, Rfbt={rfbt:g}",
        includes=(Include(path=str(library.resolve())),),
        sources=tuple(sources),
        elements=tuple(elements),
        tran=TranSpec(tstep=TSTOP / 20000.0, tstop=TSTOP, tstart=0.0, tmax=TMAX),
        save=("V(VOUT)", "V(FB)", "V(PG)", "V(EN)", "V(SW)"),
        options={"method": "gear", "trtol": 7},
    )
    return write_deck(deck, run_dir / "app.cir")


def simulate(tmp_path: Path, *, name: str, install: LtspiceInstall, **deck_options: object):
    ctx = RunContext(project_dir=tmp_path, ltspice=install.path, timeout_s=300)
    test_case = case(name, ["REQ_TPS54320_ELEC_020"])
    artifacts = run_case(
        ctx,
        test_case,
        build_deck=lambda run_dir: buck_deck(run_dir, **deck_options),  # type: ignore[arg-type]
        run_identifier=name,
    )
    return artifacts, test_case


@pytest.fixture(scope="module")
def nominal(ltspice_install: LtspiceInstall, tmp_path_factory: pytest.TempPathFactory):
    return simulate(
        tmp_path_factory.mktemp("nominal"),
        name="nominal",
        rfbt=RFBT_NOMINAL,
        install=ltspice_install,
    )


@pytest.fixture(scope="module")
def broken(ltspice_install: LtspiceInstall, tmp_path_factory: pytest.TempPathFactory):
    return simulate(
        tmp_path_factory.mktemp("broken"), name="broken", rfbt=30e3, install=ltspice_install
    )


def simulate_overload(tmp_path: Path, *, rfbt: float, install: LtspiceInstall):
    """Run the overload scenario with its own shorter, switch-free deck."""
    ctx = RunContext(project_dir=tmp_path, ltspice=install.path, timeout_s=300)
    test_case = case("overload", ["REQ_TPS54320_ELEC_020"])
    artifacts = run_case(
        ctx,
        test_case,
        build_deck=lambda run_dir: overload_deck(run_dir, rfbt=rfbt),
        run_identifier="overload",
    )
    return artifacts, test_case


def settled(raw, signal: str, *, after_fraction: float = 0.85) -> float:
    """Mean of ``signal`` over the settled tail of the run."""
    axis = raw.time_column()
    assert axis is not None
    return float(np.mean(raw.column(signal)[axis > after_fraction * TSTOP]))


def test_nominal_divider_regulates_at_the_target(nominal) -> None:
    artifacts, test_case = nominal
    assert artifacts.usability.usable, artifacts.detail
    requirement = regulation_requirement()
    result = evaluate_case(test_case, artifacts, {requirement.req_id: requirement})
    assert result.status is Status.PASS, result.detail
    assert artifacts.raw is not None
    measured = settled(artifacts.raw, "V(VOUT)")
    assert measured == pytest.approx(VOUT_NOMINAL, rel=0.03)


def test_broken_feedback_divider_is_detected(broken, nominal) -> None:
    """The output must follow the external divider, not an internal fixed value."""
    artifacts, test_case = broken
    assert artifacts.usability.usable, artifacts.detail
    assert artifacts.raw is not None
    requirement = regulation_requirement()
    result = evaluate_case(test_case, artifacts, {requirement.req_id: requirement})
    assert result.status is Status.FAIL, result.detail

    expected_broken = VREF * (1 + 30e3 / RFBB)
    measured = settled(artifacts.raw, "V(VOUT)")
    assert measured == pytest.approx(expected_broken, rel=0.10), (
        f"with Rfbt=30k the output settled at {measured:.3f} V; expected about "
        f"{expected_broken:.3f} V — a fixed internal output would hide this fault"
    )
    # The feedback node still sits at the reference: the divider really sets the output.
    assert settled(artifacts.raw, "V(FB)") == pytest.approx(VREF, rel=0.05)


def test_enable_threshold_and_rail_startup(nominal) -> None:
    artifacts, _ = nominal
    assert artifacts.raw is not None
    axis = artifacts.raw.time_column()
    assert axis is not None
    vout = artifacts.raw.column("V(VOUT)")
    assert float(np.max(np.abs(vout[axis < 0.25e-3]))) < 0.1, "the rail moved before enable"

    assertion = requirement_with(
        "REQ_TPS54320_ELEC_010",
        {
            "op": "state_dependent",
            "when": {"signal": "V(EN)", "kind": "rise_above", "value": 1.21, "unit": "V"},
            "then": {"op": "rise_above", "signal": "V(VOUT)", "value": 3.0, "unit": "V"},
        },
    )
    test_case = case("enable", [assertion.req_id])
    result = evaluate_case(test_case, artifacts, {assertion.req_id: assertion})
    assert result.status is Status.PASS, result.detail


def test_power_good_follows_the_rail(nominal) -> None:
    artifacts, _ = nominal
    assert artifacts.raw is not None
    axis = artifacts.raw.time_column()
    assert axis is not None
    pg = artifacts.raw.column("V(PG)")
    assert float(np.mean(pg[axis < 0.3e-3])) < 0.4, "power-good asserted before the rail was valid"
    assert float(np.mean(pg[axis > 0.9 * TSTOP])) > 2.0, "power-good never released"

    pg_requirement = requirement_with(
        "REQ_TPS54320_PG_050",
        {"op": "rise_above", "signal": "V(PG)", "value": 2.0, "unit": "V"},
    )
    test_case = case("pg", [pg_requirement.req_id])
    result = evaluate_case(test_case, artifacts, {pg_requirement.req_id: pg_requirement})
    assert result.status is Status.PASS, result.detail


def test_current_limit_engages_and_recovers(
    tmp_path: Path, ltspice_install: LtspiceInstall
) -> None:
    """With ILIM=1.0 A and a ~2 A demand the template must limit, then recover."""
    artifacts, _ = simulate_overload(tmp_path, rfbt=RFBT_NOMINAL, install=ltspice_install)
    assert artifacts.usability.usable, artifacts.detail
    assert artifacts.raw is not None
    axis = artifacts.raw.time_column()
    assert axis is not None
    vout = artifacts.raw.column("V(VOUT)")
    normal = float(np.mean(vout[(axis > 0.9e-3) & (axis < 1.15e-3)]))
    loaded = float(np.mean(vout[(axis > 1.5e-3) & (axis < 2.2e-3)]))
    recovered = float(np.mean(vout[axis > 3.0e-3]))
    assert normal > 2.0, f"the rail never came up ({normal:.3f} V)"
    assert loaded < 0.8 * normal, (
        f"the output did not collapse under overload (normal {normal:.3f} V, loaded {loaded:.3f} V)"
        " — protection is not engaging"
    )
    assert recovered > 0.9 * normal, f"the rail did not recover ({recovered:.3f} V)"


def test_capability_gate_blocks_unprobed_behaviours(nominal) -> None:
    """A requirement needs a probed *supported* behaviour; unknown/not_tested is not a licence."""
    artifacts, test_case = nominal
    requirement = regulation_requirement()
    states: dict[str, BehaviorState] = {key: "supported" for key in BEHAVIOR_KEYS}
    model_capability = ModelCapability(
        model_id="bm_reg_buck",
        kind=ModelKind.REDUCED_BEHAVIORAL,
        behaviors=states,
        evidence_level=EvidenceLevel.SYNTHETIC_ANALYTICAL,
        source_model_hash="c" * 64,
    )
    open_gate = gate_from_capability([model_capability], {requirement.req_id: "startup"})
    assert open_gate == {}
    result = evaluate_case(
        test_case, artifacts, {requirement.req_id: requirement}, capability_gate=open_gate
    )
    assert result.status is Status.PASS, result.detail

    states["load_transients"] = "not_tested"
    gated_capability = model_capability.model_copy(update={"behaviors": states})
    closed_gate = gate_from_capability([gated_capability], {requirement.req_id: "load_transients"})
    assert requirement.req_id in closed_gate
    blocked = evaluate_case(
        test_case, artifacts, {requirement.req_id: requirement}, capability_gate=closed_gate
    )
    assert blocked.status is Status.UNKNOWN
    assert blocked.unknown_reason == "model_capability_unsupported"
    assert "load_transients" in blocked.detail


def test_vendor_capability_record_is_honest_about_what_it_could_not_probe() -> None:
    """The committed vendor capability record must not claim unprobed behaviour."""
    record = capability()
    if record is None:
        pytest.skip("no vendor capability record yet")
    assert record.evidence_level.value == "VENDOR_MODEL_COMPARED"
    assert record.behaviors["thermal_dependence"] == "unsupported"
    assert record.behaviors["compensation_loop"] == "not_tested"
    gate = gate_from_capability(
        [record],
        {"REQ_X": "compensation_loop", "REQ_Y": "thermal_dependence"},
    )
    assert set(gate) == {"REQ_X", "REQ_Y"}
