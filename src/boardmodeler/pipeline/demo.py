"""Demo board build, circuit check and fault matrix (§15, Phase 3).

The demo is the end-to-end proof: a real project is assembled from the committed
fixtures, its model library is generated, its decks are written, real LTspice runs
produce the measurements, and the results carry honest statuses.

Three entry points back the CLI:

* :func:`build_demo_project` — assemble ``fixtures/demo_board`` into a project
  directory (models, decks, tests, requirements, capability records, schematic).
* :func:`check_circuit` — run the static checks and the dynamic scenarios against
  the project and return findings, results and the coverage/HTML artifacts.
* :func:`run_fault_matrix` — mutate the project once per fault in its own
  directory, verify each fault is detected by the check it is supposed to trip,
  and prove the original project is byte-identical afterwards.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boardmodeler.domain.enums import (
    Criticality,
    EvidenceLevel,
    ModelKind,
    RequirementClass,
    RequirementKind,
    RequirementOrigin,
    Status,
)
from boardmodeler.domain.hashing import sha256_file
from boardmodeler.domain.records import (
    ExpectationSpec,
    Finding,
    ModelCapability,
    PinDefinition,
    Requirement,
    TestCase,
    TestResult,
)
from boardmodeler.models.capability import ModelProbeSpec, probe_model
from boardmodeler.models.library import ModelStore, subckt_ports
from boardmodeler.models.primitives import PRIMITIVE_PORT_ORDER, primitive_text
from boardmodeler.models.regulator import REGULATOR_PORT_ORDER, regulator_text
from boardmodeler.models.symbolism import write_symbol
from boardmodeler.pipeline.project import Project, create_project
from boardmodeler.pipeline.runner import RunContext, run_case
from boardmodeler.reporting.html import ReportInputs, ViolationMarker, write_report
from boardmodeler.schematic.mutate import MUTATORS, apply_edits, fault_ids
from boardmodeler.schematic.netlist import build_netmap, parse_netlist
from boardmodeler.schematic.neutral import NeutralProject, read_neutral_project
from boardmodeler.schematic.static_check import run_static_checks
from boardmodeler.simulation.deck import DeckSpec, Include, MeasSpec, Source, TranSpec, write_deck
from boardmodeler.simulation.ltspice import LtspiceInstall, locate, netlist_step
from boardmodeler.verification.engine import evaluate_case, gate_from_capability
from boardmodeler.verification.scenarios import all_scenario_ids, scenario

__all__ = [
    "CLOCK_STANDIN_PERIOD_S",
    "DEMO_SCENARIOS",
    "BoardModelSpec",
    "CheckResult",
    "DemoBuildResult",
    "build_demo_project",
    "check_circuit",
    "deck_for_scenario",
    "run_fault_matrix",
    "stimulus_for",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_SOURCE = REPO_ROOT / "fixtures" / "demo_board"
SWITCH_SOURCE = REPO_ROOT / "fixtures" / "switch_fixture"

DOC_ID = "doc_synthetic_switch_contract"
BOARD_DOC_ID = "doc_demo_board_requirements"


@dataclass(frozen=True)
class BoardModelSpec:
    """How one board component is instantiated in a deck."""

    model_id: str
    subckt: str
    ports: tuple[str, ...]
    params: Mapping[str, str]
    kind: str  # "regulator" | "primitive"


BOARD_MODELS: dict[str, BoardModelSpec] = {
    "bm_reg_buck": BoardModelSpec(
        "bm_reg_buck",
        "BM_REG_BUCK",
        REGULATOR_PORT_ORDER["BM_REG_BUCK"],
        {"ILIM_MODE": "0", "VREF": "0.8", "CSS": "1n", "ILIM": "3.0", "RDISCHARGE": "10"},
        "regulator",
    ),
    "bm_reg_ldo": BoardModelSpec(
        "bm_reg_ldo",
        "BM_REG_LDO",
        REGULATOR_PORT_ORDER["BM_REG_LDO"],
        # VOUT_NOM is the 1.8 V rail this instance actually regulates: the model
        # default (3.3 V) would place PG_1V8's window on the wrong rail and hold
        # the 1V8 power-good pin low for good.
        {"VREF": "0.8", "VOUT_NOM": "1.8", "CSS": "1n", "ILIM": "1.0", "RDISCHARGE": "10"},
        "regulator",
    ),
    "bm_pg_3v3": BoardModelSpec(
        "bm_pg_3v3",
        "BM_RESET_SUP",
        PRIMITIVE_PORT_ORDER["BM_RESET_SUP"],
        # Holds PERST# low while the 3V3 power-good pin is below threshold and
        # releases it 2 ms after it is good — the reset contract REQ_DEMO_SEQ_003/004.
        # (BM_PG would invert this: it asserts its output *when* the sense is good.)
        {"VTH": "1.0", "VHYS": "0.1", "TD": "2m"},
        "primitive",
    ),
    "bm_pg_1v8": BoardModelSpec(
        "bm_pg_1v8",
        "BM_RESET_SUP",
        PRIMITIVE_PORT_ORDER["BM_RESET_SUP"],
        # Holds PERST# low while the 1V8 power-good pin is below threshold and
        # releases it 2 ms after it is good — the reset contract REQ_DEMO_SEQ_003/004.
        # (BM_PG would invert this: it asserts its output *when* the sense is good.)
        {"VTH": "1.0", "VHYS": "0.1", "TD": "2m"},
        "primitive",
    ),
    "bm_load_3v3": BoardModelSpec(
        "bm_load_3v3",
        "BM_LOAD",
        PRIMITIVE_PORT_ORDER["BM_LOAD"],
        {"I_STATIC": "0.25", "I_STEP": "0.15", "T_STEP": "2m"},
        "primitive",
    ),
    "bm_load_1v8": BoardModelSpec(
        "bm_load_1v8",
        "BM_LOAD",
        PRIMITIVE_PORT_ORDER["BM_LOAD"],
        {"I_STATIC": "0.4", "I_STEP": "0.2", "T_STEP": "2m"},
        "primitive",
    ),
}

#: Scenario id -> (deck name, description) the demo can actually run.
DEMO_SCENARIOS: dict[str, str] = {
    "nominal_startup": "nominal power-up with both rails and the reset release",
    "staggered_rails": "rail sequencing with the load steps applied",
    "slow_rail": "slow input ramp",
    "fast_rail": "fast input ramp",
    "reset_early_release": "fault: reset released before its prerequisites",
    "pullup_missing": "fault: open-drain pull-up removed",
    "pullup_wrong_domain": "fault: pull-up on the wrong supply domain",
    "invalid_strap": "fault: undocumented strap word",
    "load_step": "rail step loading",
    "brownout_short_interrupt": "short input dip",
}

# The behavioural board models contain discontinuous B-sources and open-drain
# switches, so the solver takes small internal steps; a 10 ms window with a 20 us
# cap keeps a full scenario under a minute of wall time while still covering
# start-up, sequencing, the reset release and the strap sampling window.
#: Which capability behaviour each board requirement's verdict actually depends on.
#: ``REQ_DEMO_SEQ_001`` is a *steady-state* window claim, so it depends on
#: ``dc_regulation`` (probed as supported for both generated regulators); a scenario
#: whose stimulus moves a rail inside that window additionally depends on
#: ``load_transients``, which neither template establishes — see
#: :func:`_transient_sensitive_requirements`.
BEHAVIOUR_MAP: dict[str, str] = {
    "REQ_DEMO_SEQ_001": "dc_regulation",
}

SIM_STOP = 10e-3
SIM_TMAX = 20e-6
#: Truncation-error tolerance. The board models contain discontinuous behavioural
#: sources, so the default forces very small steps for no assertion-relevant gain.
DEMO_TRTOL = 20
VIN_RAMP = 2e-3


@dataclass
class DemoBuildResult:
    """What the build produced."""

    project_dir: Path
    files: list[str] = field(default_factory=list)
    decks: dict[str, Path] = field(default_factory=dict)
    tests: list[TestCase] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    pins: list[PinDefinition] = field(default_factory=list)
    static_findings: list[Finding] = field(default_factory=list)
    capabilities: dict[str, ModelCapability] = field(default_factory=dict)
    detail: str = ""


@dataclass
class CheckResult:
    """Outcome of ``circuit check``."""

    project_dir: Path
    findings: list[Finding] = field(default_factory=list)
    results: list[TestResult] = field(default_factory=list)
    status: Status = Status.UNKNOWN
    report_path: Path | None = None
    results_path: Path | None = None
    coverage: dict[str, object] = field(default_factory=dict)
    detail: str = ""

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# fixture assembly


def _merge_pins(demo_pins: Path, switch_pins: Path) -> list[dict[str, str]]:
    """Demo pin map plus the switch fixture's pins, remapped to ``U5``."""
    rows: list[dict[str, str]] = []
    for path in (demo_pins, switch_pins):
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        header = lines[0].split(",")
        for line in lines[1:]:
            row = dict(zip(header, line.split(","), strict=True))
            if path == switch_pins:
                row["refdes"] = "U5"
            rows.append(row)
    return rows


def _pin_definitions(rows: Iterable[Mapping[str, str]]) -> list[PinDefinition]:
    pins: list[PinDefinition] = []
    for row in rows:
        pins.append(
            PinDefinition.model_validate(
                {
                    "part_id": "SYNTH-BOARD",
                    "physical_pin": row["physical_pin"],
                    "name": row["name"].strip() or row["physical_pin"],
                    "function": f"board fixture pin {row['name']}",
                    "polarity": row["polarity"] or "not_applicable",
                    "direction": row["direction"],
                    "supply_domain": row["supply_domain"] or None,
                    "output_topology": row["output_topology"],
                    "connection_requirement": row["connection_requirement"],
                    "unused_pin_treatment": row["unused_pin_treatment"] or None,
                    "behavior": [],
                    "evidence": [
                        {
                            "doc_id": DOC_ID,
                            "section": "pins.csv",
                            "excerpt": f"{row['refdes']},{row['physical_pin']},{row['name']}",
                            "extraction": "synthetic_fixture",
                        }
                    ],
                    "mapped_symbol_pin": row["name"].strip() or row["physical_pin"],
                }
            )
        )
    return pins


def _pins_by_refdes(rows: Iterable[Mapping[str, str]]) -> dict[str, list[PinDefinition]]:
    grouped: dict[str, list[PinDefinition]] = {}
    for row, pin in zip(rows, _pin_definitions(rows), strict=False):
        grouped.setdefault(row["refdes"], []).append(pin)
    return grouped


def board_requirements() -> list[Requirement]:
    """The board owner's requirements (origin=USER) for the demo board."""
    common: dict[str, Any] = {
        "applies_to": "demo_board",
        "configuration": None,
        "origin": RequirementOrigin.USER,
        "criticality": Criticality.CRITICAL,
        "conditions": [],
        "conflicts": [],
        "citation_verified": True,
        "status": "active",
    }

    def requirement(
        suffix: str,
        *,
        kind: RequirementKind,
        req_class: RequirementClass,
        statement: str,
        limits: dict[str, Any] | None,
        expression: dict[str, Any] | None,
        signals: Sequence[str],
        criticality: Criticality = Criticality.CRITICAL,
    ) -> Requirement:
        return Requirement.model_validate(
            {
                **common,
                "req_id": f"REQ_DEMO_{suffix}",
                "kind": kind,
                "class": req_class,
                "criticality": criticality,
                "statement": statement,
                "limits": limits,
                "expression": expression,
                "signal_refs": list(signals),
                "evidence": [
                    {
                        "doc_id": BOARD_DOC_ID,
                        "page": None,
                        "section": "demo board requirements",
                        "table": None,
                        "figure": None,
                        "excerpt": statement[:380],
                        "extraction": "synthetic_fixture",
                    }
                ],
            }
        )

    return [
        requirement(
            "SEQ_001",
            kind=RequirementKind.ELECTRICAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="Both rails stay inside their fixture windows from 4 ms to 10 ms.",
            limits={"min": 1.71, "max": 3.465, "unit": "V"},
            expression={
                "op": "all_of",
                "items": [
                    {
                        "op": "between",
                        "signal": "V(3V3)",
                        "low": 3.135,
                        "high": 3.465,
                        "unit": "V",
                        "interval": {"start_s": 4e-3, "end_s": 10e-3},
                    },
                    {
                        "op": "between",
                        "signal": "V(1V8)",
                        "low": 1.710,
                        "high": 1.890,
                        "unit": "V",
                        "interval": {"start_s": 4e-3, "end_s": 10e-3},
                    },
                ],
            },
            signals=("V(3V3)", "V(1V8)"),
        ),
        requirement(
            "SEQ_002",
            kind=RequirementKind.TEMPORAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="The 1V8 rail comes up after the 3V3 rail, which comes up after the input.",
            limits=None,
            expression={
                "op": "ordering",
                "first": {"signal": "V(3V3)", "kind": "rise_above", "value": 3.0, "unit": "V"},
                "then": {"signal": "V(1V8)", "kind": "rise_above", "value": 1.6, "unit": "V"},
            },
            signals=("V(3V3)", "V(1V8)"),
        ),
        requirement(
            "SEQ_003",
            kind=RequirementKind.TEMPORAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="PERST# is released at least 1 ms after the last prerequisite (both PGs).",
            limits={"min": 1e-3, "max": 0.1, "unit": "s"},
            expression={
                "op": "event_delay",
                "start": {"signal": "V(PG_1V8)", "kind": "rise_above", "value": 1.0, "unit": "V"},
                "end": {"signal": "V(PERST_N)", "kind": "rise_above", "value": 2.0, "unit": "V"},
                "min_s": 1e-3,
                "max_s": 0.1,
            },
            signals=("PERST_N", "PG_1V8"),
        ),
        requirement(
            "SEQ_004",
            kind=RequirementKind.TEMPORAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="PERST# stays low until both power-good signals are asserted.",
            limits=None,
            expression={
                "op": "ordering",
                "first": {"signal": "V(PG_3V3)", "kind": "rise_above", "value": 1.0, "unit": "V"},
                "then": {"signal": "V(PERST_N)", "kind": "rise_above", "value": 2.0, "unit": "V"},
            },
            signals=("PERST_N", "PG_3V3"),
        ),
        requirement(
            "CONN_005",
            kind=RequirementKind.CONNECTIVITY,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="The sideband lines idle high on their 3V3 pull-ups.",
            limits={"min": 2.97, "unit": "V"},
            expression={
                "op": "all_of",
                "items": [
                    {"op": "rise_above", "signal": "V(SMB_CLK)", "value": 2.97, "unit": "V"},
                    {"op": "rise_above", "signal": "V(SMB_DAT)", "value": 2.97, "unit": "V"},
                ],
            },
            signals=("SMB_CLK", "SMB_DAT"),
            criticality=Criticality.IMPORTANT,
        ),
        requirement(
            "CONN_006",
            kind=RequirementKind.CONNECTIVITY,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="Both sideband pull-ups sit on the 3V3 domain.",
            limits=None,
            expression={
                # The pull-up's return net is asserted where it is observable: a
                # passive's pins carry no names in the parsed netlist, so the
                # requirement is the idle *level* being the 3V3 one and not above
                # it (a 5 V or 12 V reference shows up here immediately).
                "op": "all_of",
                "items": [
                    {"op": "rise_above", "signal": "V(SMB_CLK)", "value": 2.97, "unit": "V"},
                    {"op": "lt", "signal": "V(SMB_CLK)", "value": 3.6, "unit": "V"},
                    {"op": "lt", "signal": "V(SMB_DAT)", "value": 3.6, "unit": "V"},
                ],
            },
            signals=("SMB_DAT",),
        ),
        requirement(
            "STRAP_011",
            kind=RequirementKind.CONNECTIVITY,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="Each switch strap pin is connected to the net its level is read on.",
            limits=None,
            expression={
                "op": "all_of",
                "items": [
                    {"op": "net_equals", "refdes": "U5", "pin": "CONFIG0", "net": "CONFIG0"},
                    {"op": "net_equals", "refdes": "U5", "pin": "CONFIG1", "net": "CONFIG1"},
                    {"op": "net_equals", "refdes": "U5", "pin": "CONFIG2", "net": "CONFIG2"},
                ],
            },
            signals=("CONFIG0", "CONFIG1", "CONFIG2"),
        ),
        requirement(
            "RESET_012",
            kind=RequirementKind.CONNECTIVITY,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="The PERST# pull-up is referenced to the 3V3 domain: PERST# idles at "
            "the 3V3 level and never above it.",
            limits=None,
            expression={
                "op": "all_of",
                "items": [
                    {"op": "rise_above", "signal": "V(PERST_N)", "value": 2.97, "unit": "V"},
                    {"op": "lt", "signal": "V(PERST_N)", "value": 3.6, "unit": "V"},
                ],
            },
            signals=("PERST_N",),
        ),
        requirement(
            "CONFIG_007",
            kind=RequirementKind.FUNCTIONAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="The strap word sampled between 1 ms and 5 ms after 1V8 is valid (not 111).",
            limits=None,
            expression={
                "op": "not",
                "item": {
                    "op": "all_of",
                    "items": [
                        {"op": "rise_above", "signal": "V(CONFIG0)", "value": 1.2, "unit": "V"},
                        {"op": "rise_above", "signal": "V(CONFIG1)", "value": 1.2, "unit": "V"},
                        {"op": "rise_above", "signal": "V(CONFIG2)", "value": 1.2, "unit": "V"},
                    ],
                },
            },
            signals=("CONFIG0", "CONFIG1", "CONFIG2"),
        ),
        requirement(
            "CONFIG_008",
            kind=RequirementKind.TEMPORAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="The CONFIG0 strap does not change after its sampling window closes.",
            limits=None,
            expression={
                "op": "hold",
                "signal": "V(CONFIG0)",
                "value": 1.8,
                "unit": "V",
                "interval": {"start_s": 5e-3, "end_s": 10e-3},
                "stable": True,
            },
            signals=("CONFIG0",),
            criticality=Criticality.IMPORTANT,
        ),
        requirement(
            "CLK_009",
            kind=RequirementKind.SYSTEM,
            req_class=RequirementClass.ASSUMPTION,
            statement="Clock availability is assumed: no clock-dependent conclusion may be a PASS.",
            limits=None,
            expression=None,
            signals=("REFCLK_100M",),
        ),
        requirement(
            "DIAG_010",
            kind=RequirementKind.FUNCTIONAL,
            req_class=RequirementClass.USER_REQUIREMENT,
            statement="PRECONDITIONS_SATISFIED is a diagnostic, not proof the device booted.",
            limits=None,
            expression={
                "op": "rise_above",
                "signal": "V(PRECONDITIONS_SATISFIED)",
                "value": 1.5,
                "unit": "V",
            },
            signals=("PRECONDITIONS_SATISFIED",),
            criticality=Criticality.INFORMATIONAL,
        ),
    ]


def _write_models(out: Path) -> dict[str, Path]:
    """Write one self-contained library per board model and register it."""
    store = ModelStore(out)
    written: dict[str, Path] = {}
    for model_id, spec in BOARD_MODELS.items():
        if spec.kind == "regulator":
            text = regulator_text(spec.subckt)
        else:
            header = f"* {model_id}: {spec.subckt} from the primitive library\n"
            text = header + primitive_text(spec.subckt)
        record = store.add_text_artifact(
            text,
            model_id=model_id,
            kind="generated",
            notes=[f"subcircuit {spec.subckt}"],
        )
        written[model_id] = record.absolute(out)
    return written


def _write_symbols(out: Path, *, library: str = "models/board.lib") -> dict[str, Path]:
    """One ``.asy`` per board model, bound to the exported board library.

    The ``SpiceModel`` value is project-root relative because LTspice resolves it
    from the schematic's location at netlist time, and because SC008 requires an
    export to stay inside the project root.
    """
    written: dict[str, Path] = {}
    for model_id, spec in BOARD_MODELS.items():
        written[model_id] = write_symbol(
            out / "models" / "symbols" / f"{spec.subckt}.asy",
            spec.subckt,
            list(spec.ports),
            model_file=library,
            description=f"{model_id} (generated by boardmodeler)",
        )
    return written


def _symbol_orders(neutral: NeutralProject) -> dict[str, list[str]]:
    """The symbol pin order each modelled refdes is wired in, from its model spec."""
    orders: dict[str, list[str]] = {}
    for refdes, model_id in sorted(neutral.model_assignments.items()):
        spec = BOARD_MODELS.get(model_id)
        if spec is not None:
            orders[refdes] = list(spec.ports)
    return orders


# --------------------------------------------------------------------------- #
# decks


def _spice_value(value: object) -> str:
    """Format one instance parameter for a SPICE card."""
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _net_pins(project: NeutralProject) -> dict[str, dict[str, str]]:
    nets: dict[str, dict[str, str]] = {}
    for connection in project.connections:
        nets.setdefault(connection.refdes, {})[connection.physical_pin] = connection.net_name
    return nets


def _model_instances(
    project: NeutralProject,
    *,
    drop: Iterable[str] = (),
    node_overrides: Mapping[str, Mapping[str, str]] | None = None,
    param_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, str]:
    """Instance cards for every modelled component, in the model's port order.

    ``node_overrides`` rewires named ports (a scenario's stimulus), and
    ``param_overrides`` replaces instance parameters (a load profile, a shortened
    delay). A component listed in ``drop`` is not emitted at all.
    """
    nets = _net_pins(project)
    dropped = set(drop)
    node_overrides = node_overrides or {}
    param_overrides = param_overrides or {}

    cards: dict[str, str] = {}
    for refdes, model_id in sorted(project.model_assignments.items()):
        if refdes in dropped:
            continue
        spec = BOARD_MODELS.get(model_id)
        if spec is None:
            continue
        pins = dict(nets.get(refdes, {}))
        pins.update(node_overrides.get(refdes, {}))
        missing = [port for port in spec.ports if port not in pins]
        if missing:
            raise ValueError(f"{refdes}: model {model_id} needs pins {missing}")
        nodes = " ".join(pins[port] for port in spec.ports)
        params = {**spec.params, **param_overrides.get(refdes, {})}
        params_text = " ".join(
            f"{key}={_spice_value(value)}" for key, value in sorted(params.items())
        )
        cards[refdes] = f"X{refdes} {nodes} {spec.subckt} {params_text}".strip()
    return cards


def _passive_cards(
    project: NeutralProject,
    *,
    drop: Iterable[str] = (),
    node_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, str]:
    """Element cards for passives; two-terminal parts use the CSV pin order."""
    nets = _net_pins(project)
    dropped = set(drop)
    node_overrides = node_overrides or {}
    values = {row.refdes: row.value for row in project.components}
    cards: dict[str, str] = {}
    for row in project.components:
        refdes = row.refdes
        if refdes in dropped or refdes in project.model_assignments:
            continue
        pins = dict(nets.get(refdes, {}))
        pins.update(node_overrides.get(refdes, {}))
        if not pins:
            continue
        # Deterministic pin order: '+'/A/1 first, then '-'/B/2.
        order = sorted(pins, key=lambda pin: (pin not in ("+", "A", "1"), pin))
        if len(order) != 2:
            continue
        prefix = refdes[0].upper()
        value = values.get(refdes, "")
        cards[refdes] = f"{prefix}{refdes[1:]} {pins[order[0]]} {pins[order[1]]} {value}".strip()
    return cards


def _element_cards(
    project: NeutralProject,
    *,
    drop: Iterable[str] = (),
    node_overrides: Mapping[str, Mapping[str, str]] | None = None,
    param_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, str]:
    """Every element card a deck contains, keyed by refdes.

    The independent sources are excluded: V1/V2 are rendered from
    ``DeckSpec.sources``, and emitting them here as well produced two instances
    with the same name, which LTspice rejects ("Instance with that name already
    defined") and which made four scenario runs incomplete.
    """
    drop = set(drop) | {"V1", "V2"}
    cards = _passive_cards(project, drop=drop, node_overrides=node_overrides)
    cards.update(
        _model_instances(
            project, drop=drop, node_overrides=node_overrides, param_overrides=param_overrides
        )
    )
    return cards


# --------------------------------------------------------------------------- #
# scenario stimuli
#
# Every scenario in ``verification.scenarios`` is either applied by its deck or
# recorded as not applied; the table below is the single source for both the deck
# builder and ``tests/scenario_stimulus.json``.
#
# Two fixture facts shape every deck:
#
# * ``REFCLK_100M`` is a stand-in. The fixture declares clock *availability* an
#   assumption (``REQ_DEMO_CLK_009``) and V2 an abstraction boundary, and no
#   requirement asserts the clock rate. Driving 100 MHz with 1 ns edges for the
#   whole 10 ms window forces ~1e6 timesteps and a >120 MB ``.raw``, so every
#   deck drives a documented slow pulse instead; ``clock_missing`` and
#   ``clock_late`` differ from that stand-in exactly as their scenario says.
# * ``BM_LOAD`` has no supply-validity gate: it sinks ``I_STATIC`` even while its
#   rail is unpowered, which drags the rail negative and trips the regulator's
#   current-limit latch (measured: the rail settles at -2.5 V and the converter
#   never recovers inside the window). Every deck therefore applies its load as a
#   step (``I_STATIC=0``, ``T_STEP`` after the rail is up), and a deck that
#   collapses its rail omits the load rather than letting it sink from a dead
#   rail.

CLOCK_STANDIN_PERIOD_S = 10e-6  # 100 kHz
CLOCK_STANDIN_EDGE_S = 100e-9
CLOCK_STANDIN_WIDTH_S = 5e-6
CLOCK_STANDIN_NOTE = (
    "REFCLK_100M is a documented stand-in (100 kHz, 100 ns edges, period "
    f"{CLOCK_STANDIN_PERIOD_S:g} s): the fixture declares clock availability an ASSUMPTION "
    "(REQ_DEMO_CLK_009) and V2 an abstraction boundary, so no clock-dependent conclusion is "
    "a PASS and no deck drives the 100 MHz silicon rate."
)
LOAD_NOTE = (
    "BM_LOAD has no supply-validity gate, so each load is applied as a step (I_STATIC=0) "
    "after its rail is up; a deck that collapses its rail omits the load instead of letting "
    "it sink from a dead rail."
)
LDO_NOTE = (
    "U2 declares VOUT_NOM=1.8 so its power-good window is the 1.8 V rail it regulates "
    "(the model default 3.3 would hold PG_1V8 low for good)."
)


def _vin_ramp(rise_s: float) -> tuple[tuple[float, float], ...]:
    """A 0 -> 12 V ramp with a real rise time, held past the scenario window."""
    return ((0.0, 0.0), (rise_s, 12.0), (rise_s + 10e-3, 12.0))


def _nominal_loads(
    project: NeutralProject, *, step_times: Mapping[str, float] | None = None
) -> dict[str, dict[str, str]]:
    """The nominal load profile, taken from the project the deck is built for.

    ``loads.I1``/``loads.I2`` are the nominal currents; each is applied as a
    step at ``timing.load_step_3v3_s``/``load_step_1v8_s``. It has to be a step
    from zero (``I_STATIC=0``): ``BM_LOAD`` sinks ``I_STATIC`` even while its rail
    is unpowered, so a static term drags a starting rail negative. Everything
    lives in the neutral ``project.json`` so that the circuit description is the
    single source: a deck that carried its own copy would ignore an edit to the
    project, and the fault matrix's load mutators would inject a change nothing
    simulated. ``step_times`` overrides only the step instant, for the scenarios
    that declare a different one.
    """
    declared = project.loads or {}
    timing = project.timing
    specs = (
        ("I1", 0.25, "load_step_3v3_s", 2.5e-3),
        ("I2", 0.4, "load_step_1v8_s", 3e-3),
    )
    profile: dict[str, dict[str, str]] = {}
    for refdes, current_default, time_key, time_default in specs:
        if step_times is not None and refdes not in step_times:
            continue
        profile[refdes] = {
            "I_STATIC": "0",
            "I_STEP": _spice_value(float(declared.get(refdes, current_default))),
            "T_STEP": _spice_value(
                float((step_times or {}).get(refdes, timing.get(time_key, time_default)))
            ),
        }
    return profile


@dataclass(frozen=True)
class ClockStimulus:
    """How one deck drives the ``REFCLK_100M`` stand-in."""

    delay_s: float = 0.0
    held_low: bool = False


@dataclass(frozen=True)
class ScenarioStimulus:
    """One scenario's injection, as data the deck builder consumes.

    ``loads`` is the BM_LOAD profile keyed by refdes; ``None`` (the default) means
    the nominal profile and ``{}`` means the loads are omitted. ``load_step_s``
    overrides the step instant of the nominal profile (a subset of refdes).
    """

    applied: bool = True
    note: str = ""
    vin: tuple[tuple[float, float], ...] | None = None
    clock: ClockStimulus = ClockStimulus()
    loads: Mapping[str, Mapping[str, str]] | None = None
    load_step_s: Mapping[str, float] | None = None
    drop: frozenset[str] = frozenset()
    node_overrides: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    param_overrides: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    extra_sources: tuple[Source, ...] = ()
    extra_elements: tuple[str, ...] = ()
    initial_conditions: Mapping[str, float] = field(default_factory=dict)
    comments: tuple[str, ...] = ()


SCENARIO_STIMULI: dict[str, ScenarioStimulus] = {
    "nominal_startup": ScenarioStimulus(
        note="2 ms VIN ramp, EN released by the input divider, loads at their nominal "
        "currents once their rails are up",
    ),
    "nominal_shutdown": ScenarioStimulus(
        vin=((0.0, 0.0), (2e-3, 12.0), (6e-3, 12.0), (7e-3, 0.0), (12e-3, 0.0)),
        loads={},
        note="VIN falls from 12 V to 0 V between 6 ms and 7 ms, so both converters stop and "
        "the outputs discharge through the declared discharge path; the loads are omitted "
        "because the rails collapse",
    ),
    "slow_rail": ScenarioStimulus(
        vin=_vin_ramp(8e-3),
        loads={},
        note="VIN ramps over 8 ms, four times the nominal rise; the loads are omitted because "
        "both rails are still ramping inside the 10 ms window",
    ),
    "fast_rail": ScenarioStimulus(
        vin=_vin_ramp(0.4e-3),
        note="VIN ramps over 0.4 ms, five times faster than nominal",
    ),
    "staggered_rails": ScenarioStimulus(
        node_overrides={"U2": {"EN": "EN_U2_DLY"}},
        extra_elements=("X_seq PG_3V3 EN_U2_DLY 3V3 0 BM_DELAY TD=1m TR=100n",),
        loads=None,
        load_step_s={"I1": 2.5e-3, "I2": 4.5e-3},
        note="U2's EN is driven from PG_3V3 through a 1 ms BM_DELAY, so the 1V8 rail ramps "
        "after the 3V3 rail with a declared 1 ms separation",
    ),
    "en_before_vin": ScenarioStimulus(
        vin=_vin_ramp(8e-3),
        extra_sources=(Source.dc("V_en_early", "EN_U1", "0", 3.3),),
        note="EN is held above its threshold from t=0 by an ideal source while VIN ramps over "
        "8 ms, so enable is present long before VIN reaches its UVLO threshold",
    ),
    "pg_delayed": ScenarioStimulus(
        node_overrides={"U1": {"PG": "PG_3V3_RAW"}},
        extra_elements=(
            "R7r PG_3V3_RAW 3V3 20k",
            "X_pgdelay PG_3V3_RAW PG_3V3 3V3 0 BM_DELAY TD=5m TR=100n",
        ),
        loads=None,
        load_step_s={"I1": 2.5e-3, "I2": 9e-3},
        note="U1's PG output is routed through a 5 ms BM_DELAY (R7r keeps the open-drain pin "
        "pulled up while delayed) before it reaches the dependent logic, so PG_3V3 asserts "
        "about 5 ms later than in the nominal deck",
    ),
    "pg_missing": ScenarioStimulus(
        node_overrides={"U3": {"pg_in": "0"}},
        note="the PG_3V3 net is removed from U3's input: the pin is tied to ground, so the "
        "reset supervisor sees no PG at all and its output is released for the whole run",
    ),
    "pullup_missing": ScenarioStimulus(
        drop=frozenset({"R17"}),
        note="R17, the 3V3 pull-up that defines SMB_DAT's open-drain idle level, is removed "
        "from the element list, so SMB_DAT has no pull-up and sits at 0 V",
    ),
    "pullup_wrong_domain": ScenarioStimulus(
        node_overrides={"R17": {"B": "1V8"}},
        note="R17 is moved from the 3V3 domain to the 1V8 rail, so SMB_DAT idles at 1.8 V "
        "instead of its declared 3V3 level",
    ),
    "reset_early_release": ScenarioStimulus(
        param_overrides={"U3": {"TD": "200u"}, "U4": {"TD": "200u"}},
        # The nominal load steps (2.5 ms on 3V3, 3 ms on 1V8) land inside the
        # reset-release window: the PG glitches they cause discharge the reset
        # supervisor's TD lag network and re-assert PERST#, so the shortened
        # hold this scenario injects never reaches the output. The declared
        # amplitudes are still applied, after the reset window has closed.
        load_step_s={"I1": 6e-3, "I2": 8e-3},
        note="U3's assertion delay, the reset hold after its PG input becomes valid, is "
        "shortened from 2 ms to 200 us; the nominal load steps are moved after the "
        "reset-release window so a load-step PG glitch cannot hide the injected fault",
    ),
    "brownout_short_interrupt": ScenarioStimulus(
        vin=(
            (0.0, 0.0),
            (2e-3, 12.0),
            (3.5e-3, 12.0),
            (3.6e-3, 6.0),
            (4.1e-3, 6.0),
            (4.2e-3, 12.0),
            (12e-3, 12.0),
        ),
        loads={},
        note="VIN dips from 12 V to 6 V for 0.5 ms (3.5 ms to 4.2 ms): the dip stays above "
        "the buck's UVLO_FALL of 3.9 V, so the rails must ride through it. The loads are "
        "omitted because the reduced behavioural models have no supply-validity gate: a "
        "constant-current load on a sagging rail drags the output negative (measured "
        "-0.33 V), which is a model artefact, not a board behaviour, and would be "
        "reported as a rail violation",
    ),
    "prebiased_output": ScenarioStimulus(
        initial_conditions={"V(3V3)": 1.0},
        note="the 3V3 output starts pre-charged to 1.0 V while the input ramps and EN is "
        "released by the divider; the pre-bias hold-off must keep the converter from sinking "
        "the pre-charge",
    ),
    "load_step": ScenarioStimulus(
        extra_elements=("X_load_step 3V3 GND BM_LOAD I_STATIC=0 I_STEP=0.5 T_STEP=6m",),
        note="an additional 0.5 A load step is applied to the 3V3 rail at 6 ms on top of the "
        "nominal 0.25 A load",
    ),
    "overload": ScenarioStimulus(
        extra_elements=("X_overload 3V3 GND BM_LOAD I_STATIC=0 I_STEP=4 T_STEP=6m",),
        note="an additional 4 A load, above the buck's 3 A ILIM, is applied to the 3V3 rail "
        "at 6 ms; the declared protection mode is hiccup with an 8 ms retry, so the retry "
        "falls outside the 10 ms window",
    ),
    "partial_power": ScenarioStimulus(
        node_overrides={"U2": {"EN": "0"}},
        extra_sources=(Source.dc("V_1v8_clamp", "1V8", "0", 0.0),),
        loads=None,
        load_step_s={"I1": 2.5e-3},
        note="the 1V8 domain is unpowered: U2's EN is tied to ground so the LDO stays off and "
        "an ideal clamp holds the 1V8 rail at 0 V while the 3V3 domain runs normally",
    ),
    "externally_driven_unpowered_pin": ScenarioStimulus(
        node_overrides={"U2": {"EN": "0"}},
        extra_sources=(
            Source.dc("V_1v8_clamp", "1V8", "0", 0.0),
            Source.dc("V_ext_drive", "CONFIG0", "0", 3.3),
        ),
        loads=None,
        load_step_s={"I1": 2.5e-3},
        note="the 1V8 domain is unpowered (U2 disabled, rail clamped to 0 V) while an external "
        "3.3 V driver holds its CONFIG0 strap pin high: a drive on a dead domain. U5 has no "
        "model, so the drive level, not a back-power current, is what the deck shows",
    ),
    "invalid_strap": ScenarioStimulus(
        extra_sources=(Source.ramp("V_strap_cfg1", "CONFIG1", "0", v0=0.0, v1=1.8, rise_s=100e-6),),
        note="CONFIG1 is driven high by an ideal source at start-up, making the sampled strap "
        "word 111, outside the documented set (101)",
    ),
    "late_strap": ScenarioStimulus(
        extra_sources=(
            Source(
                name="V_strap_cfg0",
                terminals=("CONFIG0", "0"),
                kind="pwl",
                points=((0.0, 1.8), (6e-3, 1.8), (6.01e-3, 0.0), (12e-3, 0.0)),
            ),
        ),
        note="CONFIG0 is held at its sampled 1.8 V level and then driven to 0 V at 6 ms, after "
        "the documented sampling window (1-5 ms) has closed",
    ),
    "clock_missing": ScenarioStimulus(
        clock=ClockStimulus(held_low=True),
        note="the REFCLK_100M source is held at 0 V instead of the stand-in pulse, so no clock "
        "edge exists anywhere in the window",
    ),
    "clock_late": ScenarioStimulus(
        clock=ClockStimulus(delay_s=6e-3),
        note="the REFCLK_100M stand-in pulse is delayed to 6 ms, after the start of the "
        "declared clock-arrival budget",
    ),
    "repeat_power_cycles": ScenarioStimulus(
        vin=(
            (0.0, 0.0),
            (1e-3, 12.0),
            (3e-3, 12.0),
            (4e-3, 0.0),
            (5e-3, 0.0),
            (6e-3, 12.0),
            (8e-3, 12.0),
            (9e-3, 0.0),
            (12e-3, 0.0),
        ),
        loads={},
        note="two complete 12 V power cycles inside the window (0-4 ms and 5-9 ms); the loads "
        "are omitted because the rails collapse twice",
    ),
    "incomplete_discharge": ScenarioStimulus(
        vin=(
            (0.0, 0.0),
            (2e-3, 12.0),
            (3.2e-3, 12.0),
            (3.3e-3, 0.0),
            (3.9e-3, 0.0),
            (4.2e-3, 12.0),
            (12e-3, 12.0),
        ),
        loads={},
        note="VIN is removed for 0.9 ms and re-applied at 4.2 ms while the 3V3 output capacitor "
        "is still discharging, so the converter is re-enabled during discharge",
    ),
}


def stimulus_for(scenario_id: str) -> ScenarioStimulus:
    """The stimulus record for one scenario; an unknown id is an error."""
    try:
        return SCENARIO_STIMULI[scenario_id]
    except KeyError as exc:
        raise KeyError(f"no stimulus is defined for scenario {scenario_id!r}") from exc


def _clock_source(clock: ClockStimulus) -> Source:
    if clock.held_low:
        return Source.dc("V2", "REFCLK_100M", "0", 0.0)
    return Source.pulse(
        "V2",
        "REFCLK_100M",
        "0",
        v1=0.0,
        v2=3.3,
        delay_s=clock.delay_s,
        rise_s=CLOCK_STANDIN_EDGE_S,
        fall_s=CLOCK_STANDIN_EDGE_S,
        width_s=CLOCK_STANDIN_WIDTH_S,
        period_s=CLOCK_STANDIN_PERIOD_S,
    )


def _ic_card(initial_conditions: Mapping[str, float]) -> str:
    pairs = " ".join(f"{node}={_spice_value(value)}" for node, value in initial_conditions.items())
    return f".ic {pairs}"


@dataclass(frozen=True)
class DeckStimulus:
    """The concrete cards one deck carries, plus the record of what they are."""

    specification: ScenarioStimulus
    sources: tuple[Source, ...]
    cards: Mapping[str, str]
    injection: str
    comments: tuple[str, ...]


def _stimulus_cards(
    project: NeutralProject,
    stimulus: ScenarioStimulus,
    *,
    include_loads: bool,
) -> dict[str, str]:
    """The element cards a deck built from ``stimulus`` contains."""
    if stimulus.loads is None:
        loads = _nominal_loads(project, step_times=stimulus.load_step_s)
    else:
        loads = {refdes: dict(params) for refdes, params in stimulus.loads.items()}
    param_overrides: dict[str, dict[str, object]] = {
        refdes: dict(params) for refdes, params in stimulus.param_overrides.items()
    }
    pg_delay = project.timing.get("pg_delay_s")
    if pg_delay is not None:
        for refdes, model_id in project.model_assignments.items():
            spec = BOARD_MODELS.get(model_id)
            if spec is not None and spec.subckt == "BM_RESET_SUP":
                param_overrides.setdefault(refdes, {}).setdefault(
                    "TD", _spice_value(float(pg_delay))
                )
    for refdes, params in loads.items():
        param_overrides.setdefault(refdes, {}).update(params)
    drop = set(stimulus.drop)
    if include_loads:
        drop |= {"I1", "I2"} - set(loads)
    else:
        drop |= {"I1", "I2"}
    return _element_cards(
        project,
        drop=drop,
        node_overrides=stimulus.node_overrides,
        param_overrides=param_overrides,
    )


def _stimulus_plan(
    project: NeutralProject, scenario_id: str, *, include_loads: bool = True
) -> DeckStimulus:
    """Resolve one scenario's deck cards and the exact injection they carry."""
    spec = scenario(scenario_id)
    stimulus = stimulus_for(scenario_id)
    sources = (
        Source(
            name="V1",
            terminals=("VIN_12V_SRC", "0"),
            kind="pwl",
            points=stimulus.vin if stimulus.vin is not None else _vin_ramp(VIN_RAMP),
        ),
        _clock_source(stimulus.clock),
        *stimulus.extra_sources,
    )
    cards = _stimulus_cards(project, stimulus, include_loads=include_loads)
    nominal_cards = _stimulus_cards(
        project, SCENARIO_STIMULI["nominal_startup"], include_loads=include_loads
    )

    injection: list[str] = []
    if stimulus.applied:
        injection = [source.card() for source in sources]
        injection.extend(stimulus.extra_elements)
        for refdes, card in sorted(nominal_cards.items()):
            if refdes in ("V1", "V2"):
                continue
            if refdes not in cards:
                injection.append(f"* removed: {card}")
            elif cards[refdes] != card:
                injection.append(f"* changed: {card} -> {cards[refdes]}")
        if stimulus.initial_conditions:
            injection.append(_ic_card(stimulus.initial_conditions))

    comments = [
        f"* scenario {scenario_id}: {spec.title} ({spec.kind}, intent {spec.intent})",
        f"* declared stimulus: {', '.join(spec.stimulus) if spec.stimulus else 'none'}",
    ]
    for line in injection:
        comments.append(f"* injection: {line}")
    if stimulus.note:
        comments.append(f"* note: {stimulus.note}")
    comments.extend(f"* {line}" for line in stimulus.comments)
    comments.append(f"* {CLOCK_STANDIN_NOTE}")
    comments.append(f"* {LOAD_NOTE}")
    comments.append(f"* {LDO_NOTE}")

    return DeckStimulus(
        specification=stimulus,
        sources=tuple(sources),
        cards=cards,
        injection="\n".join(injection) if injection else "none",
        comments=tuple(comments),
    )


def deck_for_scenario(
    project: NeutralProject,
    scenario_id: str,
    *,
    out: Path,
    include_loads: bool = True,
    extra_elements: Sequence[str] = (),
) -> DeckSpec:
    """Build the deck for one scenario from the neutral netlist."""
    spec = scenario(scenario_id)
    plan = _stimulus_plan(project, scenario_id, include_loads=include_loads)

    elements: list[str] = [card for _, card in sorted(plan.cards.items())]
    elements.extend(plan.specification.extra_elements)
    elements.extend(extra_elements)

    # The diagnostic signal is computed, never a pin of the device.
    elements.append(
        "Bdiag PRECONDITIONS_SATISFIED 0 V=if(V(PERST_N)>2 & V(3V3)>3.1 & V(1V8)>1.7, 3.3, 0)"
    )

    library = out / "models" / "board.lib"
    includes = (Include(path=str(library.resolve())),)
    directives = [
        "* U5 (SYNTH-PCIE-SW-0) has no model: its lanes and protocol are outside dynamic "
        "coverage; its supplies, reset, straps and sideband pins are checked statically.",
        *plan.comments,
    ]
    if plan.specification.initial_conditions:
        directives.append(_ic_card(plan.specification.initial_conditions))

    return DeckSpec(
        title=f"demo board - {scenario_id} ({spec.title})",
        includes=includes,
        sources=plan.sources,
        elements=tuple(elements),
        directives=tuple(directives),
        tran=TranSpec(tstep=SIM_STOP / 20000.0, tstop=SIM_STOP, tstart=0.0, tmax=SIM_TMAX),
        save=(
            "V(3V3)",
            "V(1V8)",
            "V(12V)",
            "V(PERST_N)",
            "V(PG_3V3)",
            "V(PG_1V8)",
            "V(CONFIG0)",
            "V(CONFIG1)",
            "V(CONFIG2)",
            "V(SMB_CLK)",
            "V(SMB_DAT)",
            "V(CLK_REQ_N)",
            "V(PRECONDITIONS_SATISFIED)",
            "V(EN_U1)",
            "V(FB_U1)",
            "V(REFCLK_100M)",
        ),
        meas=(
            MeasSpec(name="v3v3_9ms", signal="V(3V3)", at_s=9e-3),
            MeasSpec(name="v1v8_9ms", signal="V(1V8)", at_s=9e-3),
            MeasSpec(name="vperst_9ms", signal="V(PERST_N)", at_s=9e-3),
        ),
        options={"method": "gear", "trtol": DEMO_TRTOL},
    )


def _combined_library(out: Path) -> Path:
    """Concatenate the per-model libraries, defining each subcircuit once.

    Every per-model library is self-contained (it embeds the primitives it
    instantiates), so a plain concatenation would define `BM_SCHMITT` several
    times and LTspice would reject the deck. Duplicate definitions are dropped
    when they are byte-identical and reported as an error when they are not.
    """
    store = ModelStore(out)
    blocks: dict[str, str] = {}
    order: list[str] = []
    for model_id in sorted(BOARD_MODELS):
        text = store.read_text(model_id)
        for block in _split_subcircuits(text):
            name = _subckt_name_of(block)
            if name is None:
                continue
            existing = blocks.get(name)
            if existing is None:
                blocks[name] = block
                order.append(name)
            elif existing.strip() != block.strip():
                raise ValueError(
                    f"two different definitions of subcircuit {name!r} would be exported in "
                    "one library"
                )
    parts = ["* demo board model library (generated by boardmodeler)"]
    parts.append(
        "* each subcircuit is defined once; the per-model files remain in models/generated/"
    )
    parts.extend(blocks[name] for name in order)
    library = out / "models" / "board.lib"
    library.write_text("\n\n".join(part.rstrip() for part in parts) + "\n", encoding="utf-8")
    return library


def _split_subcircuits(text: str) -> list[str]:
    """Every ``.subckt … .ends`` block in ``text``, in file order."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(".subckt"):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif stripped.startswith(".ends"):
            current.append(line)
            blocks.append("\n".join(current))
            current = []
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _subckt_name_of(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(".subckt"):
            parts = stripped.split()
            return parts[1] if len(parts) > 1 else None
    return None


# --------------------------------------------------------------------------- #
# build


def build_demo_project(
    out_dir: str | Path, *, ltspice: LtspiceInstall | None = None, workdir: Path | None = None
) -> DemoBuildResult:
    """Assemble the demo project from the committed fixtures."""
    out = Path(out_dir).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    create_project(
        out,
        project_id="demo_board",
        name="Integrated board-level demonstration",
        mode="circuit",
        supply_domains=json.loads((DEMO_SOURCE / "circuit" / "project.json").read_text())[
            "supply_domains"
        ],
        notes=[
            "built by `boardmodeler demo build` from fixtures/demo_board and fixtures/switch_fixture"
        ],
    )

    # circuit files
    (out / "circuit").mkdir(exist_ok=True)
    for name in ("components.csv", "connections.csv", "project.json"):
        shutil.copyfile(DEMO_SOURCE / "circuit" / name, out / "circuit" / name)
    pin_rows = _merge_pins(DEMO_SOURCE / "pins.csv", SWITCH_SOURCE / "pins.csv")
    header = (
        "refdes,physical_pin,name,direction,polarity,supply_domain,output_topology,"
        "connection_requirement,unused_pin_treatment"
    )
    (out / "circuit" / "pins.csv").write_text(
        header
        + "\n"
        + "\n".join(
            ",".join(
                [
                    row["refdes"],
                    row["physical_pin"],
                    row["name"],
                    row["direction"],
                    row["polarity"],
                    row["supply_domain"],
                    row["output_topology"],
                    row["connection_requirement"],
                    row["unused_pin_treatment"],
                ]
            )
            for row in pin_rows
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # documents + requirements
    docs = out / "docs" / "files"
    docs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        SWITCH_SOURCE / "SYNTHETIC_SWITCH_CONTRACT.md",
        docs / f"{DOC_ID}-SYNTHETIC_SWITCH_CONTRACT.md",
    )
    switch_requirements = json.loads(
        (SWITCH_SOURCE / "requirements.json").read_text(encoding="utf-8")
    )
    requirements = [
        Requirement.model_validate(item) for item in switch_requirements["requirements"]
    ] + board_requirements()
    (out / "evidence").mkdir(exist_ok=True)
    (out / "evidence" / "requirements.json").write_text(
        json.dumps(
            {
                "requirements": [
                    json.loads(r.model_dump_json(by_alias=True)) for r in requirements
                ],
                "note": "device contract (TEST_FIXTURE) plus board requirements (USER)",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "evidence" / "pinmap.json").write_text(
        json.dumps({"pins": pin_rows}, indent=2), encoding="utf-8"
    )

    # models
    models_dir = out / "models"
    models_dir.mkdir(exist_ok=True)
    for folder in ("generated", "records", "capabilities"):
        (models_dir / folder).mkdir(exist_ok=True)
    _write_models(out)
    _combined_library(out)
    symbols = _write_symbols(out)

    # decks + tests
    decks_dir = out / "tests" / "decks"
    decks_dir.mkdir(parents=True, exist_ok=True)
    neutral = read_neutral_project(out)
    tests: list[TestCase] = []
    for index, scenario_id in enumerate(DEMO_SCENARIOS, start=1):
        deck = deck_for_scenario(neutral, scenario_id, out=out)
        write_deck(deck, decks_dir / f"{scenario_id}.cir")
        bound = _requirements_for(scenario_id, requirements)
        if not bound:
            # A case with no requirement would be evaluated against nothing and
            # report UNKNOWN forever; that is a build defect, not a result.
            raise ValueError(
                f"scenario {scenario_id!r} is bound to no requirement, so its case could "
                "never produce a verdict"
            )
        tests.append(
            TestCase(
                test_id=f"T_{scenario_id}_{index:03d}",
                requirement_ids=_requirements_for(scenario_id, requirements),
                scenario_id=scenario_id,
                scope="circuit_compliance"
                if scenario(scenario_id).intent == "satisfy"
                else "fault_detection",
                deck_template=f"tests/decks/{scenario_id}.cir",
                expected=ExpectationSpec(
                    kind=scenario(scenario_id).intent, detail=scenario(scenario_id).description
                ),
                measurement=[
                    "V(3V3)",
                    "V(1V8)",
                    "V(PERST_N)",
                    "V(PG_3V3)",
                    "V(PG_1V8)",
                    "V(CONFIG0)",
                    "V(SMB_CLK)",
                    "V(SMB_DAT)",
                ],
                max_timestep_s=SIM_TMAX,
            )
        )
    (out / "tests" / "tests.json").write_text(
        json.dumps({"tests": [json.loads(t.model_dump_json()) for t in tests]}, indent=2),
        encoding="utf-8",
    )

    # Every scenario declares what its deck injects, or that it injects nothing.
    # Written for all known scenario ids, not only the ones with a test case, so a
    # reader can see which stimuli exist and which the demonstration actually runs.
    scenario_record: dict[str, dict[str, object]] = {}
    for scenario_id in all_scenario_ids():
        try:
            plan = _stimulus_plan(neutral, scenario_id)
        except KeyError as exc:  # pragma: no cover - every id is in the table
            scenario_record[scenario_id] = {
                "applied": False,
                "injection": "none",
                "note": f"no stimulus is defined: {exc}",
                "has_test_case": False,
            }
            continue
        scenario_record[scenario_id] = {
            "applied": plan.specification.applied,
            "injection": plan.injection,
            "note": plan.specification.note,
            "has_test_case": scenario_id in DEMO_SCENARIOS,
        }
    (out / "tests" / "scenario_stimulus.json").write_text(
        json.dumps(scenario_record, indent=2, sort_keys=True), encoding="utf-8"
    )
    unapplied = [sid for sid, entry in scenario_record.items() if not entry["applied"]]

    # static checks on the built netlist
    pins_by_refdes = _pins_by_refdes(pin_rows)
    circuit = _circuit_from_neutral(neutral, pins_by_refdes)
    netmap = build_netmap(circuit)
    symbol_orders = _symbol_orders(neutral)
    static_findings = run_static_checks(
        circuit,
        netmap,
        project=neutral,
        pins=pins_by_refdes,
        symbol_pins=symbol_orders,
        symbol_texts={
            refdes: symbols[neutral.model_assignments[refdes]]
            for refdes in symbol_orders
            if refdes in neutral.model_assignments
        },
        project_root=out,
    )
    (out / "evidence" / "static_findings.json").write_text(
        json.dumps([json.loads(f.model_dump_json()) for f in static_findings], indent=2),
        encoding="utf-8",
    )

    # capability records for the generated models
    install = ltspice or locate()
    capabilities: dict[str, ModelCapability] = {}
    if install is not None and workdir is not None:
        capabilities = _probe_board_models(out, install, workdir)

    result = DemoBuildResult(
        project_dir=out,
        tests=tests,
        requirements=requirements,
        static_findings=static_findings,
        capabilities=capabilities,
        decks={sid: decks_dir / f"{sid}.cir" for sid in DEMO_SCENARIOS},
        files=sorted(
            str(path.relative_to(out)).replace("\\", "/")
            for path in out.rglob("*")
            if path.is_file()
        ),
        detail=(
            f"{len(requirements)} requirements, {len(tests)} test cases, "
            f"{len(static_findings)} static findings, "
            f"{len(scenario_record) - len(unapplied)}/{len(scenario_record)} scenario stimuli applied"
            + (f" (not applied: {', '.join(sorted(unapplied))})" if unapplied else "")
        ),
    )
    (out / "evidence" / "demo_build.json").write_text(
        json.dumps(
            {
                "detail": result.detail,
                "requirements": len(requirements),
                "tests": [t.test_id for t in tests],
                "static_findings": [
                    {"code": f.code, "status": f.status.value, "refdes": f.refdes}
                    for f in static_findings
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return result


def _requirements_for(scenario_id: str, requirements: Sequence[Requirement]) -> list[str]:
    """Which requirements a scenario exercises.

    Operating scenarios exercise the rail/sequencing/connectivity requirements;
    fault scenarios target the specific requirement whose violation they inject,
    so a fault scenario that fails to trip anything is visible as such.
    """
    by_suffix = {"_".join(r.req_id.split("_")[-2:]): r.req_id for r in requirements}
    if scenario(scenario_id).intent == "violate_detected":
        mapping = {
            "reset_early_release": ["SEQ_003", "SEQ_004"],
            "pullup_missing": ["CONN_005"],
            "pullup_wrong_domain": ["CONN_006", "CONN_005"],
            "early_reset_release": ["RESET_012", "SEQ_003"],
            "invalid_strap": ["CONFIG_007", "STRAP_011"],
            "swap_straps": ["STRAP_011", "CONFIG_007"],
            "load_step": ["SEQ_001"],
            "brownout_short_interrupt": ["SEQ_001"],
        }.get(scenario_id, [])
        return [by_suffix[suffix] for suffix in mapping if suffix in by_suffix]
    # A scenario may only be bound to a requirement it can actually exercise.
    # `slow_rail` ramps its input over 8 ms, so the steady rail window (which
    # opens at 4 ms) is not a claim that scenario can support: binding it there
    # would report the stimulus itself as a rail violation.
    if scenario_id == "slow_rail":
        # An 8 ms input ramp reaches regulation after the strap window has closed,
        # so the only claim this scenario can carry is the rail *order*.
        return [by_suffix[suffix] for suffix in ("SEQ_002",) if suffix in by_suffix]
    operating = [
        "SEQ_001",
        "SEQ_002",
        "SEQ_003",
        "SEQ_004",
        "CONN_005",
        "CONFIG_007",
        "CONFIG_008",
        "DIAG_010",
        "STRAP_011",
        "RESET_012",
    ]
    return [by_suffix[suffix] for suffix in operating if suffix in by_suffix]


def _circuit_from_neutral(
    project: NeutralProject, pins_by_refdes: Mapping[str, Sequence[PinDefinition]] | None = None
):
    """A netlist-level circuit for the static checks (X instances + passives).

    Components with no simulation model are emitted too, as instances of a stub
    subcircuit built from their pin list: an abstraction boundary only means
    something if the part it describes is actually in the netlist the checks read.
    """
    lines = ["* demo board netlist for static checks"]
    emitted: set[str] = set()
    nets: dict[str, dict[str, str]] = {}
    for connection in project.connections:
        nets.setdefault(connection.refdes, {})[connection.physical_pin] = connection.net_name
    for refdes, model_id in sorted(project.model_assignments.items()):
        spec = BOARD_MODELS.get(model_id)
        if spec is None:
            continue
        pins = nets.get(refdes, {})
        nodes = " ".join(pins.get(port, "NC") for port in spec.ports)
        lines.append(f"X{refdes} {nodes} {spec.subckt}")
        if spec.subckt not in emitted:
            lines.append(f".subckt {spec.subckt} {' '.join(spec.ports)}")
            lines.append(f".ends {spec.subckt}")
            emitted.add(spec.subckt)
    for row in project.components:
        pins = nets.get(row.refdes, {})
        if not pins or row.refdes in project.model_assignments:
            continue
        order = sorted(pins, key=lambda pin: (pin not in ("+", "A", "1"), pin))
        if len(pins) > 2:
            # an unmodelled multi-pin part: keep it in the netlist with its own
            # stub subcircuit so every pin is visible to the checks
            pin_defs = list((pins_by_refdes or {}).get(row.refdes, []))
            port_order = [
                (pin.name, pin.physical_pin) for pin in pin_defs if pin.physical_pin in pins
            ] or [(pin, pin) for pin in sorted(pins)]
            subckt = row.part_number or f"UNMODELLED_{row.refdes}"
            nodes = " ".join(pins[physical] for _name, physical in port_order)
            lines.append(f"X{row.refdes} {nodes} {subckt}")
            if subckt not in emitted:
                lines.append(f".subckt {subckt} " + " ".join(name for name, _ in port_order))
                lines.append(f".ends {subckt}")
                emitted.add(subckt)
            continue
        if len(order) != 2:
            continue
        first, second = pins[order[0]], pins[order[1]]
        if row.refdes == "V1":
            lines.append(f"V1 {first} 0 PWL(0 0 2m 12 20m 12)")
            continue
        if row.refdes == "V2":
            lines.append(f"V2 {first} 0 PULSE(0 3.3 0 1n 1n 5n 10n)")
            continue
        prefix = row.refdes[0].upper()
        lines.append(f"{prefix}{row.refdes[1:]} {first} {second} {row.value}")
    return parse_netlist("\n".join(lines) + "\n.end\n")


def _probe_board_models(
    out: Path, install: LtspiceInstall, workdir: Path
) -> dict[str, ModelCapability]:
    """Probe the generated regulator models and record their capabilities."""
    store = ModelStore(out)
    capabilities: dict[str, ModelCapability] = {}
    ctx = RunContext(project_dir=out, ltspice=install.path, timeout_s=120)
    for model_id in ("bm_reg_buck", "bm_reg_ldo"):
        spec = BOARD_MODELS[model_id]
        model_path = store.path_of(model_id)
        text = model_path.read_text(encoding="utf-8")
        ports = subckt_ports(text, spec.subckt)
        role_of_port = _roles_for(spec.subckt, ports)
        probe_spec = ModelProbeSpec(
            model_id=model_id,
            kind=ModelKind.REDUCED_BEHAVIORAL,
            path=model_path,
            subckt=spec.subckt,
            ports=tuple(ports),
            port_roles=role_of_port,
            nets={"gnd": "0", "vout": "n_vout", "vin": "n_vin", "sw": "n_sw"},
            nominal_vout=3.3 if "BUCK" in spec.subckt else 1.8,
            vref=0.8,
            rfbt=10e3,
            rfbb=3.24e3 if "BUCK" in spec.subckt else 8.06e3,
            vin=12.0 if "BUCK" in spec.subckt else 3.3,
            load_ohm=10.0,
            probe_time_scale=1.0,
        )
        report = probe_model(
            probe_spec,
            ctx,
            workdir=workdir / model_id,
            evidence_level=EvidenceLevel.SYNTHETIC_ANALYTICAL,
            exclusions=[
                "generated reduced behavioural model, not silicon-accurate",
                "temperature dependence is not modelled",
            ],
        )
        capabilities[model_id] = report.capability
        (out / "models" / "capabilities" / f"{model_id}.json").write_text(
            report.capability.model_dump_json(indent=2), encoding="utf-8"
        )
    # requirement -> behaviour map used by the capability gate
    requirement_behaviours = {
        "REQ_DEMO_SEQ_001": "dc_regulation",
        "REQ_DEMO_SEQ_002": "startup",
        "REQ_DEMO_SEQ_003": "startup",
        "REQ_DEMO_SEQ_004": "startup",
    }
    (out / "evidence" / "capability_map.json").write_text(
        json.dumps(requirement_behaviours, indent=2), encoding="utf-8"
    )
    return capabilities


def _roles_for(subckt: str, ports: Sequence[str]) -> dict[str, str]:
    mapping = {
        "VIN": "vin",
        "EN": "en",
        "FB": "fb",
        "PG": "pg",
        "VOUT": "vout",
        "GND": "gnd",
        "SW": "sw",
        "ILIM_MODE": "ilim_mode",
        "COMP": "comp",
        "BOOT": "boot",
        "SS": "ss",
        "RT": "rt",
    }
    return {port: mapping[port] for port in ports if port in mapping}


# --------------------------------------------------------------------------- #
# circuit check


def check_circuit(
    project_dir: str | Path,
    *,
    circuit_path: Path | None = None,
    ltspice: LtspiceInstall | None = None,
    scope: str | None = None,
    cancel: threading.Event | None = None,
    report_path: Path | None = None,
    results_path: Path | None = None,
) -> CheckResult:
    """Static checks + dynamic scenarios for a built demo project."""
    root = Path(project_dir).resolve()
    project = Project(root)
    neutral = read_neutral_project(root)
    install = ltspice or locate()

    pin_rows = (
        [
            {key: value for key, value in row.items()}
            for row in json.loads((root / "evidence" / "pinmap.json").read_text(encoding="utf-8"))[
                "pins"
            ]
        ]
        if (root / "evidence" / "pinmap.json").is_file()
        else []
    )
    pins_by_refdes = _pins_by_refdes(pin_rows)

    findings: list[Finding] = []
    circuit = _circuit_from_neutral(neutral)
    netmap = build_netmap(circuit)
    # Symbols are part of what SC005/SC006 check; without them those checks can
    # only answer NOT_APPLICABLE, which would understate what was verified.
    symbol_orders = _symbol_orders(neutral)
    symbol_dir = root / "models" / "symbols"
    symbol_texts = {
        refdes: symbol_dir / f"{BOARD_MODELS[model_id].subckt}.asy"
        for refdes, model_id in sorted(neutral.model_assignments.items())
        if model_id in BOARD_MODELS
        and (symbol_dir / f"{BOARD_MODELS[model_id].subckt}.asy").is_file()
    }
    findings.extend(
        run_static_checks(
            circuit,
            netmap,
            project=neutral,
            pins=pins_by_refdes,
            symbol_pins=symbol_orders,
            symbol_texts=symbol_texts,
            project_root=root,
        )
    )

    if circuit_path is not None and install is not None:
        step = netlist_step(install.path, Path(circuit_path), timeout_s=120)
        findings.append(
            Finding(
                code="SC001_syntax",
                status=Status.PASS if step.exit_code == 0 else Status.FAIL,
                message=(
                    f"LTspice netlisted {Path(circuit_path).name} successfully"
                    if step.exit_code == 0
                    else f"LTspice could not netlist {Path(circuit_path).name}: {step.observed()}"
                ),
                detail={"deck": str(circuit_path)},
            )
        )

    # The engine indexes requirements by id, while the report/coverage helpers
    # take a sequence: keep both views of the same loaded set.
    requirement_map = project.requirements()
    requirements = list(requirement_map.values())
    tests = project.tests(scope=scope)
    capabilities = _load_capabilities(root)
    behaviour_map = _load_behaviour_map(root)
    capability_gate = (
        gate_from_capability(list(capabilities.values()), behaviour_map)
        if capabilities and behaviour_map
        else None
    )

    results: list[TestResult] = []
    if install is not None:
        ctx = project.run_context(ltspice=install, timeout_s=180)
        for case in tests:
            case_gate = dict(capability_gate or {})
            case_gate.update(
                gate_from_capability(
                    list(capabilities.values()),
                    _transient_sensitive_requirements(neutral, case),
                )
            )
            if cancel is not None and cancel.is_set():
                break
            # The deck is rebuilt from the circuit that is on disk *now*, not
            # copied from the artefacts of the last build: a mutated circuit
            # (the fault matrix) would otherwise be simulated with the unmutated
            # deck while its static checks saw the mutation, and the dynamic
            # half of every fault verdict would be a claim about a different
            # circuit than the one being reported on. The committed
            # `tests/decks/<scenario>.cir` remains as the build's record and is
            # the fallback for a project whose neutral files are absent (an
            # exported copy).
            deck_path = root / case.deck_template
            artifacts = run_case(
                ctx,
                case,
                build_deck=lambda run_dir, deck_path=deck_path, case=case: _deck_for_case(
                    neutral, case, deck_path, run_dir, root
                ),
                run_identifier=case.test_id,
                cancel=cancel,
            )
            results.append(
                evaluate_case(
                    case,
                    artifacts,
                    requirement_map,
                    connectivity=netmap,
                    supply_domains=neutral.supply_domains,
                    capability_gate=case_gate or None,
                )
            )

    status = _worst(findings, results)
    result = CheckResult(
        project_dir=root,
        findings=findings,
        results=results,
        status=status,
        coverage=_coverage(requirements, tests, results),
        detail=f"{len(findings)} finding(s), {len(results)} result(s)",
    )

    if results_path is not None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            json.dumps(
                {
                    "project": str(root),
                    "status": status.value,
                    "findings": [json.loads(f.model_dump_json()) for f in findings],
                    "results": [json.loads(r.model_dump_json()) for r in results],
                    "summary": result.summary(),
                    "coverage": result.coverage,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result.results_path = results_path

    if report_path is not None:
        markers: dict[str, list[ViolationMarker]] = {}
        for test_result in results:
            if test_result.status is not Status.FAIL:
                continue
            for requirement_id in test_result.requirement_ids:
                for signal in ("V(3V3)", "V(1V8)", "V(PERST_N)"):
                    markers.setdefault(signal, []).append(
                        ViolationMarker(
                            requirement_id=requirement_id, t_s=6e-3, label=test_result.detail[:80]
                        )
                    )
        write_report(
            report_path,
            ReportInputs(
                project_id=project.config.project_id,
                project_name=project.config.name,
                status=status,
                findings=findings,
                results=results,
                requirements=requirements,
                coverage=result.coverage,
                capability=next(iter(capabilities.values()), None),
                limitations=[
                    "U5 (the synthetic switch) has no model: lanes and protocol are outside dynamic coverage",
                    "clock availability is an assumption, not a verified path",
                    "temperature dependence is not modelled anywhere",
                ],
                qualifications=["fixture-derived results, not silicon measurements"],
                reproduction=[
                    "uv run boardmodeler demo build --out build/demo",
                    "uv run boardmodeler circuit check --project build/demo "
                    "--circuit build/demo/circuit/demo.asc --json",
                ],
                extra_sections={"Violation markers": _markers_table(markers)},
            ),
        )
        result.report_path = report_path
    return result


def _markers_table(markers: Mapping[str, Sequence[ViolationMarker]]) -> str:
    if not markers:
        return "<p class='muted'>No failing requirement produced a marker.</p>"
    rows = "".join(
        f"<tr><td><code>{signal}</code></td><td>{marker.requirement_id}</td>"
        f"<td>{marker.t_s:g} s</td><td>{marker.label}</td></tr>"
        for signal, items in sorted(markers.items())
        for marker in items
    )
    return (
        "<table><thead><tr><th>signal</th><th>requirement</th><th>time</th><th>detail</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _deck_for_case(
    neutral: NeutralProject, case: TestCase, committed: Path, run_dir: Path, root: Path
) -> Path:
    """Write the deck this case must actually run.

    Rebuilt from ``neutral`` when the scenario is known, because that is what makes
    a circuit mutation visible to the simulation; otherwise the committed deck is
    copied (an export, or a circuit the demo does not assemble).
    """
    scenario_id = case.scenario_id or ""
    if scenario_id and (root / "models" / "board.lib").is_file():
        try:
            deck = deck_for_scenario(neutral, scenario_id, out=root)
        except KeyError, ValueError:
            pass
        else:
            return write_deck(deck, run_dir / "deck.cir")
    return _copy_deck(committed, run_dir)


def _copy_deck(source: Path, run_dir: Path) -> Path:
    target = run_dir / "deck.cir"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _load_capabilities(root: Path) -> dict[str, ModelCapability]:
    folder = root / "models" / "capabilities"
    if not folder.is_dir():
        return {}
    return {
        path.stem: ModelCapability.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
    }


#: The requirement whose window the scenarios measure rails over, and the time that
#: window opens: a load step at or after it is part of the measured interval.
RAIL_WINDOW_REQUIREMENT = "REQ_DEMO_SEQ_001"
RAIL_WINDOW_START_S = 4e-3
#: The strap must not move after its sampling window closes; its stability rides on
#: the rail that pulls it up, so the same transient gate applies to it.
STRAP_STABILITY_REQUIREMENT = "REQ_DEMO_CONFIG_008"


def _perturbs_a_rail_inside_the_window(deck_text: str) -> bool:
    """Does this deck move a rail after the measured window opens?

    Two shapes count: a load step at or after the window start, and an input source
    whose own PWL turns back down inside the window (a drawn dip or brownout). Both
    make the case a large-signal transient question rather than a steady-state one.
    """
    for token in re.findall(r"T_STEP=([0-9.eE+-]+)", deck_text):
        try:
            if float(token) >= RAIL_WINDOW_START_S:
                return True
        except ValueError:  # pragma: no cover - generated card
            continue
    for card in re.findall(r"^V\w+\s+\S+\s+\S+\s+PWL\(([^)]*)\)", deck_text, re.MULTILINE):
        numbers = [float(value) for value in card.split()]
        points = list(zip(numbers[0::2], numbers[1::2], strict=False))
        highest = float("-inf")
        for time_s, value in points:
            if time_s >= RAIL_WINDOW_START_S and value < highest:
                return True
            highest = max(highest, value)
    return False


def _transient_sensitive_requirements(neutral: NeutralProject, case: TestCase) -> dict[str, str]:
    """Requirements whose verdict that *case* cannot support on this model.

    A scenario that moves a rail inside the requirement's window asks for the model's
    large-signal response, not its steady-state regulation: the probe records
    ``load_transients`` as ``unknown`` for both generated regulators (D-013). Judging
    such a case would report a model limitation as a circuit failure, so it is gated
    to UNKNOWN with ``model_capability_unsupported`` instead.

    The gate is per case: the *nominal* deck perturbs nothing inside the window, and
    its steady-state claim stands on the probed ``dc_regulation`` support. The
    requirements gated are the rail window itself and the strap-stability claim,
    because a strap held up by a rail follows that rail when it moves.
    """
    bound = set(case.requirement_ids)
    sensitive = [
        rid for rid in (RAIL_WINDOW_REQUIREMENT, STRAP_STABILITY_REQUIREMENT) if rid in bound
    ]
    if not sensitive:
        return {}
    try:
        deck_text = deck_for_scenario(neutral, case.scenario_id, out=neutral.root).render(
            neutral.root
        )
    except KeyError, ValueError:
        return {}
    if not _perturbs_a_rail_inside_the_window(deck_text):
        return {}
    return {rid: "load_transients" for rid in sensitive}


def _load_behaviour_map(root: Path) -> dict[str, str]:
    path = root / "evidence" / "capability_map.json"
    if not path.is_file():
        return {}
    return {
        str(key): str(value) for key, value in json.loads(path.read_text(encoding="utf-8")).items()
    }


def _worst(findings: Sequence[Finding], results: Sequence[TestResult]) -> Status:
    order = [Status.BLOCKED, Status.FAIL, Status.UNKNOWN, Status.NOT_APPLICABLE, Status.PASS]
    seen = {finding.status for finding in findings} | {result.status for result in results}
    for status in order:
        if status in seen:
            return status
    return Status.UNKNOWN


def _coverage(
    requirements: Sequence[Requirement], tests: Sequence[TestCase], results: Sequence[TestResult]
) -> dict[str, object]:
    from boardmodeler.reporting.export import requirements_coverage

    return requirements_coverage(requirements, tests, results)


# --------------------------------------------------------------------------- #
# fault matrix


def run_fault_matrix(
    project_dir: str | Path,
    *,
    out_dir: Path | None = None,
    ltspice: LtspiceInstall | None = None,
    faults: Sequence[str] | None = None,
) -> dict[str, object]:
    """Inject every fault into its own copy, run the check, and record detection.

    The original project is hashed before and after, so a mutation that leaked into
    it is detected rather than assumed away.
    """
    root = Path(project_dir).resolve()
    work = Path(out_dir) if out_dir is not None else root.parent / "fault_matrix"
    work.mkdir(parents=True, exist_ok=True)

    watched = ["circuit/connections.csv", "circuit/components.csv", "circuit/project.json"]
    before = {name: sha256_file(root / name) for name in watched if (root / name).is_file()}

    baseline = check_circuit(root, ltspice=ltspice)

    entries: list[dict[str, object]] = []
    for fault_id in faults or fault_ids():
        variant = work / fault_id
        mutation = MUTATORS[fault_id](root)
        apply_edits(root, mutation.edits, variant, fault_id=fault_id)
        check = check_circuit(variant, ltspice=ltspice)
        detected = _detected(check, baseline)
        entries.append(
            {
                "fault_id": fault_id,
                "description": mutation.description,
                "expected_detection": mutation.expected_detection,
                "detected": detected,
                "status": check.status.value,
                "summary": check.summary(),
                "evidence": _detection_evidence(check, baseline),
                "modifications": [edit.as_dict() for edit in mutation.edits],
            }
        )

    after = {name: sha256_file(root / name) for name in watched if (root / name).is_file()}
    report = {
        "project": str(root),
        "faults": entries,
        "detected": sum(1 for entry in entries if entry["detected"]),
        "total": len(entries),
        "original_unchanged": before == after,
        "original_hashes": after,
    }
    (work / "fault_matrix.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _failing_findings(check: CheckResult) -> set[tuple[str, str | None]]:
    return {
        (finding.code, finding.refdes)
        for finding in check.findings
        if finding.status is Status.FAIL
    }


def _detected(check: CheckResult, baseline: CheckResult) -> bool:
    """Was the injected fault actually caught, relative to the unmutated project?

    A fault counts as detected only when the mutated check shows something the
    baseline check did not: a new failing static finding, a newly observed
    violation (a `violate_detected` case that now passes), or a new dynamic
    failure. Comparing against the baseline is what keeps a mutation that changed
    no simulated behaviour from being recorded as detected just because the check
    already had some unrelated FAIL.
    """
    if _failing_findings(check) - _failing_findings(baseline):
        return True
    baseline_status = {result.test_id: result.status for result in baseline.results}
    for result in check.results:
        before = baseline_status.get(result.test_id)
        if before is result.status:
            continue
        if result.status is Status.PASS and "violation detected" in result.detail:
            return True
        if result.status is Status.FAIL and before is not Status.FAIL:
            return True
    return False


def _detection_evidence(check: CheckResult, baseline: CheckResult | None = None) -> list[str]:
    baseline_status = (
        {result.test_id: result.status for result in baseline.results} if baseline else {}
    )
    evidence = [
        f"{finding.code} [{finding.status.value}] {finding.message[:120]}"
        for finding in check.findings
        if finding.status is not Status.PASS
    ]
    for result in check.results:
        if (
            result.status is Status.PASS
            and baseline is not None
            and baseline_status.get(result.test_id) is Status.PASS
        ):
            continue
        evidence.append(f"{result.test_id} [{result.status.value}] {result.detail[:120]}")
    return evidence[:8]
