"""The viability gate: can this model be dropped into somebody else's simulation?

A model can pass every datasheet row and still be useless in a system simulation: an
output stage that returns its current through global ground instead of the supply pins,
an output with no current limit, an input that floats far outside the rails, a package
pin missing from the symbol. This module is the part-agnostic judge of that. It never
looks at what the part *does*; it looks at how the part *sits in a circuit*, using only
its ports, so the same gate judges an op amp, a regulator and a microcontroller.

Static checks read the model text and the symbol. Dynamic checks build small benches from
the pin table and run them in LTspice. The bench holds the device's ground pin
``OFFSET_V`` above the simulator's node 0, so a model that quietly uses node 0 as its own
return path shows up as a broken Kirchhoff current law at its ports.

Honesty rules, as everywhere in this engine:

* a check is PASS only when its bench ran and the measured number is inside the band;
  a bench that could not run is UNKNOWN with the reason, never PASS;
* bands come from the pin table's cited numbers (``GatePin.isc_max``) or from physics that
  holds for every part (current conservation), never from a threshold tuned to a model;
* the gate describes a model, it never edits one.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from boardmodeler.authoring.convergence import ERROR, lint_library, parse_value
from boardmodeler.domain.enums import Status
from boardmodeler.models.library import subckt_ports
from boardmodeler.models.symbolism import symbol_pin_orders, validate_symbol
from boardmodeler.simulation.deck import TranSpec
from boardmodeler.simulation.log import parse_log
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.measures import diagnose
from boardmodeler.simulation.raw import RawFormatError, read_raw

__all__ = [
    "ALARM_KINDS",
    "PIN_KINDS",
    "GateCheck",
    "GatePin",
    "GateReport",
    "GateSpec",
    "dynamic_checks",
    "run_gate",
    "static_checks",
]

#: What a package pin is, for the bench. ``pad`` is an exposed pad: a separate pin whose
#: PCB connection is external and required, never tied inside the model (D-050).
PIN_KINDS = ("supply", "ground", "input", "output", "io", "analog", "nc", "pad")

ALARM_KINDS = ("abs", "ovl", "flt", "tie")
#: A quiet alarm reads below this, a firing one above ``ALARM_FIRES`` (the model writes 0 / 1).
ALARM_QUIET = 0.2
ALARM_FIRES = 0.8

_DRIVEN = ("input", "io", "analog")  # kinds the bench sets to floating / low / high
_GROUND_LIKE = ("ground", "pad")

#: The device's ground pin sits this far above node 0 in every dynamic bench.
OFFSET_V = 2.5
#: An output "short" is this resistance to the ground pin's net or to the supply net.
SHORT_OHMS = 0.01
#: Without a cited short-circuit rating, this much current is unphysical for any IC pin.
GENERIC_SHORT_LIMIT_A = 10.0
#: Allowed short-circuit current, as a multiple of the cited rating.
SHORT_LIMIT_FACTOR = 2.0
TSTOP_S = 5e-3
TSTEP_S = 5e-6
#: Kirchhoff residual allowed at the ports: absolute floor plus a fraction of throughput.
KCL_ABS_A = 1e-9
KCL_REL = 1e-6
#: A floating input may sit this far outside the rails before "no clamp" is reported.
RAIL_MARGIN_V = 0.5
#: Smallest supply current that counts as "the supply pins draw something".
MIN_SUPPLY_A = 1e-9
#: Output current below this is treated as "no output current was exercised".
ACTIVE_OUTPUT_A = 1e-3
#: The supply pins must cover this fraction of the current an output delivers.
SUPPLY_COVER = 0.9
SUPPLY_COVER_FLOOR_A = 1e-6
_MAX_PATTERN_PINS = 8


@dataclass(frozen=True)
class GatePin:
    """One package pin as the bench needs it."""

    port: str
    kind: str
    number: str = ""
    #: supply pins: test voltage relative to the ground pin (cite the recommended range)
    vtest: float | None = None
    #: output pins: the short-circuit current the datasheet allows, in amperes (cited)
    isc_max: float | None = None
    #: output pins with no function core: the instance parameter that commands the pin
    #: (``-1`` off, ``0``..``1`` a fraction of the rail); the gate uses it to exercise the pin
    force: str | None = None
    #: alarms the model claims on this pin: "abs" (absolute maximum), "ovl" (output overload),
    #: "flt" (undefined digital level), "tie" (required connection missing). Each claim is
    #: proven by a clean bench that must stay quiet and a fault bench that must fire.
    alarms: tuple[str, ...] = ()
    #: for an "abs" claim: the voltage above the ground pin that is past the limit
    abs_fault_v: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in PIN_KINDS:
            raise ValueError(f"pin {self.port!r}: kind {self.kind!r} is not one of {PIN_KINDS}")
        bad = [a for a in self.alarms if a not in ALARM_KINDS]
        if bad:
            raise ValueError(f"pin {self.port!r}: unknown alarm kind(s) {bad}")
        if "abs" in self.alarms and self.abs_fault_v is None:
            raise ValueError(f"pin {self.port!r}: an abs alarm needs abs_fault_v to test it")


@dataclass(frozen=True)
class GateSpec:
    """The part, as the gate sees it: a subcircuit name and its pin table."""

    part: str
    subckt: str
    pins: tuple[GatePin, ...]
    #: cited internal pin-to-pin ties (pairs of port names); none by default
    allowed_ties: tuple[tuple[str, str], ...] = ()
    #: bench length, and the largest solver step for parts that switch (None: the default)
    tstop: float = TSTOP_S
    tmax: float | None = None

    def pin(self, port: str) -> GatePin | None:
        lowered = port.lower()
        for pin in self.pins:
            if pin.port.lower() == lowered:
                return pin
        return None


@dataclass(frozen=True)
class GateCheck:
    """One verdict with the numbers behind it."""

    id: str
    status: Status
    detail: str
    measured: Mapping[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status.value,
            "detail": self.detail,
            "measured": dict(self.measured),
        }


@dataclass
class GateReport:
    part: str
    checks: list[GateCheck]
    runs: int = 0
    wall_s: float = 0.0

    @property
    def status(self) -> Status:
        statuses = {check.status for check in self.checks}
        if Status.FAIL in statuses:
            return Status.FAIL
        if Status.UNKNOWN in statuses or Status.BLOCKED in statuses:
            return Status.UNKNOWN
        return Status.PASS

    def failing(self) -> list[GateCheck]:
        return [check for check in self.checks if check.status is Status.FAIL]

    def as_dict(self) -> dict[str, object]:
        return {
            "part": self.part,
            "status": self.status.value,
            "runs": self.runs,
            "wall_s": round(self.wall_s, 2),
            "checks": [check.as_dict() for check in self.checks],
        }


# --------------------------------------------------------------------------- #
# netlist reading


def _logical_lines(text: str) -> list[tuple[int, str]]:
    joined: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        code = raw.split(";", 1)[0].strip()
        if not code or code.startswith("*"):
            continue
        if code.startswith("+") and joined:
            first, previous = joined[-1]
            joined[-1] = (first, previous + " " + code[1:].strip())
            continue
        joined.append((number, code))
    return joined


def _subckt_body(text: str, name: str) -> list[tuple[int, str]]:
    body: list[tuple[int, str]] = []
    inside = False
    for number, line in _logical_lines(text):
        words = line.split()
        head = words[0].lower()
        if head == ".subckt" and len(words) > 1 and words[1].lower() == name.lower():
            inside = True
            continue
        if head == ".ends" and inside:
            break
        if inside:
            body.append((number, line))
    return body


_NODE_COUNT = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "B": 2, "D": 2}
_NODE_COUNT.update({"E": 4, "G": 4, "F": 2, "H": 2, "S": 4, "W": 2})
_V_REF = re.compile(r"\bV\s*\(\s*([^,()\s]+)\s*(?:,\s*([^,()\s]+)\s*)?\)", re.IGNORECASE)


def _element_nodes(line: str) -> list[str]:
    words = line.split()
    kind = words[0][0].upper()
    if kind == "X":
        tokens = [w for w in words[1:] if "=" not in w and w.lower() != "params:"]
        return tokens[:-1]
    if kind == "A":
        return words[1:9]
    if kind == "Q":
        return words[1:4]
    return words[1 : 1 + _NODE_COUNT.get(kind, 0)]


# --------------------------------------------------------------------------- #
# static checks


def static_checks(lib_text: str, spec: GateSpec, *, asy_text: str | None = None) -> list[GateCheck]:
    """Checks that need only the model text (and the symbol, when there is one)."""
    checks: list[GateCheck] = []
    try:
        ports = list(subckt_ports(lib_text, spec.subckt))
    except Exception as exc:  # the model does not define the subcircuit at all
        return [
            GateCheck(
                "ports_match_pin_table",
                Status.FAIL,
                f"subcircuit {spec.subckt!r} could not be read from the model: {exc}",
            )
        ]
    lowered_ports = [p.lower() for p in ports]

    # 1. every pin of the pin table is a port, and nothing else is
    table = [p.port.lower() for p in spec.pins]
    missing = [p.port for p in spec.pins if p.port.lower() not in lowered_ports]
    extra = [p for p in ports if p.lower() not in table]
    duplicated = sorted({p for p in lowered_ports if lowered_ports.count(p) > 1})
    if missing or extra or duplicated:
        problems = []
        if missing:
            problems.append(f"missing ports {missing}")
        if extra:
            problems.append(f"ports not in the pin table {extra}")
        if duplicated:
            problems.append(f"duplicated ports {duplicated}")
        checks.append(GateCheck("ports_match_pin_table", Status.FAIL, "; ".join(problems)))
    else:
        checks.append(
            GateCheck(
                "ports_match_pin_table",
                Status.PASS,
                f"{len(ports)} ports, exactly the {len(spec.pins)} pins of the pin table",
                {"ports": float(len(ports))},
            )
        )

    # 2. the symbol lists the same pins, at the SpiceOrder that matches the port position
    if asy_text is not None:
        problems = [
            f.message for f in validate_symbol(asy_text, ports=ports) if f.status is Status.FAIL
        ]
        for name, order in symbol_pin_orders(asy_text):
            if not 1 <= order <= len(ports) or ports[order - 1].lower() != name.lower():
                at = ports[order - 1] if 1 <= order <= len(ports) else "nothing"
                problems.append(
                    f"symbol pin {name} says SpiceOrder {order}, but port {order} is {at}"
                )
        checks.append(
            GateCheck(
                "symbol_order_matches_ports",
                Status.FAIL if problems else Status.PASS,
                "; ".join(problems) if problems else "every symbol pin sits at its port position",
            )
        )

    body = _subckt_body(lib_text, spec.subckt)

    # 3. the model never leans on the simulator's global ground
    leaks: list[str] = []
    for number, line in body:
        if line.startswith("."):
            continue
        for node in _element_nodes(line):
            if node == "0" or (node.lower() == "gnd" and "gnd" not in lowered_ports):
                leaks.append(f"line {number} `{line.split()[0]}` uses node {node}")
                break
        else:
            for match in _V_REF.finditer(line):
                nodes = [n for n in match.groups() if n]
                if any(
                    n == "0" or (n.lower() == "gnd" and "gnd" not in lowered_ports) for n in nodes
                ):
                    leaks.append(f"line {number} `{line.split()[0]}` reads V({','.join(nodes)})")
                    break
    checks.append(
        GateCheck(
            "no_global_ground",
            Status.FAIL if leaks else Status.PASS,
            (
                f"{len(leaks)} element(s) use the simulator's node 0 / GND instead of a port, "
                f"so the current they carry never passes through the package pins "
                f"(first: {leaks[0]})"
                if leaks
                else "every element and expression refers only to ports and internal nodes"
            ),
            {"elements": float(len(leaks))},
        )
    )

    # 4. no low-impedance tie between two pins unless the datasheet says so
    allowed = {frozenset((a.lower(), b.lower())) for a, b in spec.allowed_ties}
    ties: list[str] = []
    for number, line in body:
        words = line.split()
        kind = words[0][0].upper()
        if kind not in ("R", "V") or len(words) < 4:
            continue
        a, b = words[1].lower(), words[2].lower()
        if a == b or a not in lowered_ports or b not in lowered_ports:
            continue
        value_token = words[4] if words[3].lower() == "dc" and len(words) > 4 else words[3]
        value = parse_value(value_token)
        if value is None or frozenset((a, b)) in allowed:
            continue
        if (kind == "R" and value <= 1.0) or (kind == "V" and value == 0.0):
            ties.append(f"line {number} `{words[0]}` joins {words[1]} and {words[2]}")
    checks.append(
        GateCheck(
            "no_internal_pin_ties",
            Status.FAIL if ties else Status.PASS,
            (
                "pins are tied together inside the model with no cited datasheet statement: "
                + "; ".join(ties)
                if ties
                else "no pin is tied to another pin inside the model"
            ),
            {"ties": float(len(ties))},
        )
    )

    # 5. every internal node has a DC path (the existing linter, errors only)
    errors = [f for f in lint_library(lib_text) if f.severity == ERROR]
    checks.append(
        GateCheck(
            "dc_paths",
            Status.FAIL if errors else Status.PASS,
            (
                f"{len(errors)} structural convergence error(s); first: {errors[0].text()}"
                if errors
                else "no structural convergence errors found by the netlist linter"
            ),
            {"errors": float(len(errors))},
        )
    )
    return checks


# --------------------------------------------------------------------------- #
# dynamic benches


@dataclass(frozen=True)
class _Run:
    name: str
    states: Mapping[str, str]  # driven port -> "float" | "low" | "high"
    short: str  # "none" | "gnd" | "vcc": every output port is shorted this way
    #: instance parameters that make outputs deliver current into the short
    params: Mapping[str, float] = field(default_factory=dict)
    #: ports driven to a voltage above the ground pin (absolute-maximum fault benches)
    over: Mapping[str, float] = field(default_factory=dict)
    #: required-connection ports left open (missing-tie fault bench)
    open_pins: tuple[str, ...] = ()


@dataclass
class _Result:
    run: _Run
    currents: dict[str, float] = field(default_factory=dict)  # port -> A into the device
    volts: dict[str, float] = field(default_factory=dict)  # port -> V at the device pin
    alarms: dict[str, float] = field(default_factory=dict)  # alarm node -> level (0 quiet, 1 fires)
    blocked: str | None = None
    convergence: str | None = None
    warnings: list[str] = field(default_factory=list)


def _kind(spec: GateSpec, port: str) -> str:
    pin = spec.pin(port)
    return pin.kind if pin is not None else "analog"


def _plan_runs(spec: GateSpec, ports: Sequence[str]) -> list[_Run]:
    driven = [p for p in ports if _kind(spec, p) in _DRIVEN]
    outputs = [p for p in ports if _kind(spec, p) == "output"]

    nc_pins = [p for p in ports if _kind(spec, p) == "nc"]

    def uniform(state: str, *, nc: bool = False) -> dict[str, str]:
        states = {p: state for p in driven}
        if nc:
            states.update({p: state for p in nc_pins})
        return states

    runs = [
        _Run("float", uniform("float"), "none"),
        _Run("low", uniform("low", nc=True), "none"),
        _Run("high", uniform("high", nc=True), "none"),
    ]
    commanded = [pin.force for pin in spec.pins if pin.force and pin.port in ports]
    commanded_outputs = [p for p in outputs if (pin := spec.pin(p)) is not None and pin.force]
    if outputs or commanded:
        patterns: list[tuple[str, dict[str, str]]] = [("low", uniform("low"))]
        if len(commanded_outputs) < len(outputs):  # an output only inputs can drive needs patterns
            patterns.append(("high", uniform("high")))
            for port in [p for p in driven if _kind(spec, p) != "analog"][:_MAX_PATTERN_PINS]:
                patterns.append((f"hot_{port}", {**uniform("low"), port: "high"}))
                patterns.append((f"cold_{port}", {**uniform("high"), port: "low"}))
        for short in ("gnd", "vcc"):
            # a commanded pin sources into a short to ground and sinks from one to supply
            params = {name: (1.0 if short == "gnd" else 0.0) for name in commanded}
            for label, states in patterns:
                runs.append(_Run(f"short_{short}_{label}", states, short, params))
    for pin in spec.pins:
        if "abs" in pin.alarms and pin.port in ports and pin.abs_fault_v is not None:
            runs.append(
                _Run(f"over_{pin.port}", uniform("low"), "none", over={pin.port: pin.abs_fault_v})
            )
    tied = tuple(pin.port for pin in spec.pins if "tie" in pin.alarms and pin.port in ports)
    if tied:
        runs.append(_Run("tie_open", uniform("low"), "none", open_pins=tied))
    return runs


def alarm_node(kind: str, port: str) -> str:
    """The internal node a model uses for alarm ``kind`` on ``port`` (0 quiet, 1 fires)."""
    return f"chk_{kind}_{port}"


def _claimed_alarms(spec: GateSpec, ports: Sequence[str]) -> list[tuple[str, str, str]]:
    lowered = {p.lower() for p in ports}
    return [
        (kind, pin.port, alarm_node(kind, pin.port))
        for pin in spec.pins
        if pin.port.lower() in lowered
        for kind in pin.alarms
    ]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _deck(lib_path: Path, spec: GateSpec, ports: Sequence[str], run: _Run) -> str:
    lines = [
        f"* viability gate: {spec.part} / {run.name}",
        f'.include "{lib_path}"',
        f"Vgnd ngnd 0 {OFFSET_V}",
    ]
    supply_nodes: list[tuple[str, float]] = []
    for i, port in enumerate(ports, start=1):
        pin = spec.pin(port)
        kind = _kind(spec, port)
        if kind == "supply":
            vtest = pin.vtest if pin is not None and pin.vtest is not None else 5.0
            vtest = run.over.get(port, vtest)
            lines.append(f"Vs{i} nsup{i} ngnd {vtest:g}")
            supply_nodes.append((f"nsup{i}", vtest))
    if supply_nodes:
        top = max(supply_nodes, key=lambda item: item[1])[0]
        lines.append(f"Rtop {top} nvcc 1u")
    else:
        lines.append("Vhi nvcc ngnd 3.3")
    for i, port in enumerate(ports, start=1):
        kind = _kind(spec, port)
        if kind == "supply":
            lines.append(f"Vm{i} nsup{i} n{i} 0")
        elif kind in _GROUND_LIKE:
            lines.append(f"Vm{i} {f'e{i}' if port in run.open_pins else 'ngnd'} n{i} 0")
        elif port in run.over:
            lines.append(f"Vo{i} nover{i} ngnd {run.over[port]:g}")
            lines.append(f"Vm{i} nover{i} n{i} 0")
        elif kind in _DRIVEN or kind == "nc":
            state = run.states.get(port, "float")
            source = {"low": "ngnd", "high": "nvcc"}.get(state, f"e{i}")
            lines.append(f"Vm{i} {source} n{i} 0")
        elif kind == "output" and run.short in ("gnd", "vcc"):
            lines.append(f"Vm{i} e{i} n{i} 0")
            lines.append(f"Rs{i} e{i} {'ngnd' if run.short == 'gnd' else 'nvcc'} {SHORT_OHMS:g}")
        else:  # an open output
            lines.append(f"Vm{i} e{i} n{i} 0")
    nodes = " ".join(f"n{i}" for i in range(1, len(ports) + 1))
    params = "".join(f" {name}={value:g}" for name, value in run.params.items())
    lines.append(f"Xdut {nodes} {spec.subckt}{params}")
    saves = " ".join(f"I(Vm{i}) V(n{i})" for i in range(1, len(ports) + 1))
    saves += "".join(f" V(xdut:{node})" for _kind_, _port_, node in _claimed_alarms(spec, ports))
    tran = _tran(spec)
    lines += [".options plotwinsize=0", tran.card(), f".save {saves}", ".end"]
    return "\n".join(lines) + "\n"


def _tran(spec: GateSpec) -> TranSpec:
    if spec.tmax is None:
        return TranSpec(tstep=TSTEP_S, tstop=spec.tstop)
    return TranSpec(tstep=TSTEP_S, tstop=spec.tstop, tstart=0.0, tmax=spec.tmax)


def _tail_mean(values: np.ndarray) -> float:
    cut = int(len(values) * 0.8)
    return float(np.mean(values[cut:] if cut < len(values) else values))


def _execute(
    lib_path: Path, spec: GateSpec, ports: Sequence[str], run: _Run, ltspice: Path, root: Path
) -> _Result:
    result = _Result(run)
    folder = root / _safe(run.name)
    folder.mkdir(parents=True, exist_ok=True)
    deck = folder / "deck.cir"
    deck.write_text(_deck(lib_path, spec, ports, run), encoding="utf-8")
    batch = run_batch(ltspice, deck, folder, timeout_s=90.0)
    log = parse_log(batch.log_path) if batch.log_path is not None else None
    raw = None
    raw_error = None
    if batch.raw_path is not None:
        try:
            raw = read_raw(batch.raw_path)
        except (RawFormatError, OSError) as exc:
            raw_error = str(exc)
    if log is None:
        result.blocked = f"LTspice produced no log ({batch.observed()})"
        return result
    diag = diagnose(log=log, raw=raw, tran=_tran(spec), raw_error=raw_error)
    result.warnings = list(diag.warnings)
    if diag.convergence_issues:
        result.convergence = "; ".join((diag.errors + diag.convergence_issues)[:2])
        return result
    reason = diag.blocked_reason()
    if reason is not None or raw is None:
        result.blocked = reason or "no waveform was produced"
        return result
    for i, port in enumerate(ports, start=1):
        for store, name in ((result.currents, f"I(Vm{i})"), (result.volts, f"V(n{i})")):
            if raw.has(name):
                value = _tail_mean(raw.column(name))
                if not math.isfinite(value):
                    result.blocked = f"{name} is not finite"
                    return result
                store[port] = value
            else:
                result.blocked = f"{name} is missing from the waveform"
                return result
    ground = next((p for p in ports if _kind(spec, p) == "ground"), None)
    v_ground = result.volts.get(ground, 0.0) if ground is not None else 0.0
    for _alarm, _port, node in _claimed_alarms(spec, ports):
        name = f"V(xdut:{node})"
        if raw.has(name):
            # an alarm is referenced to the ground pin, which the bench holds OFFSET_V above node 0
            result.alarms[node] = _tail_mean(raw.column(name)) - v_ground
    return result


def dynamic_checks(
    lib_path: Path, spec: GateSpec, ltspice: Path, workdir: Path
) -> tuple[list[GateCheck], int]:
    """Run the pin-state and short-circuit benches; return the checks and the run count."""
    text = lib_path.read_text(encoding="utf-8", errors="replace")
    ports = list(subckt_ports(text, spec.subckt))
    plan = _plan_runs(spec, ports)
    results = [_execute(lib_path, spec, ports, run, ltspice, workdir) for run in plan]
    kinds = {p: _kind(spec, p) for p in ports}
    by_kind = lambda *want: [p for p in ports if kinds[p] in want]  # noqa: E731
    supplies, grounds = by_kind("supply"), by_kind(*_GROUND_LIKE)
    outputs, nc_pins = by_kind("output"), by_kind("nc")
    checks: list[GateCheck] = []

    # a. every pin state converges
    broken = [r for r in results if r.convergence]
    unrun = [r for r in results if r.blocked]
    if broken:
        first = broken[0]
        checks.append(
            GateCheck(
                "converges_in_every_pin_state",
                Status.FAIL,
                f"{len(broken)} of {len(results)} benches did not converge; "
                f"first: {first.run.name}: {first.convergence}",
                {"failed": float(len(broken)), "runs": float(len(results))},
            )
        )
    elif unrun:
        checks.append(
            GateCheck(
                "converges_in_every_pin_state",
                Status.UNKNOWN,
                f"{len(unrun)} of {len(results)} benches gave no data; first: "
                f"{unrun[0].run.name}: {unrun[0].blocked}",
                {"runs": float(len(results))},
            )
        )
    else:
        soft = sum(1 for r in results if r.warnings)
        checks.append(
            GateCheck(
                "converges_in_every_pin_state",
                Status.PASS,
                f"all {len(results)} benches converged (floating, grounded and tied-high "
                f"inputs; outputs open and shorted to ground and to supply)"
                + (f"; {soft} needed a solver fallback" if soft else ""),
                {"runs": float(len(results)), "fallbacks": float(soft)},
            )
        )
    good = [r for r in results if r.convergence is None and r.blocked is None]

    # b. Kirchhoff at the ports
    worst_kcl, worst_run = 0.0, ""
    for r in good:
        residual = abs(sum(r.currents.values()))
        scale = sum(abs(v) for v in r.currents.values())
        excess = residual - (KCL_ABS_A + KCL_REL * scale)
        if excess > worst_kcl:
            worst_kcl, worst_run = excess, f"{r.run.name}: sum of port currents {residual:.4g} A"
    if not good:
        checks.append(GateCheck("current_conservation", Status.UNKNOWN, "no bench gave data"))
    elif worst_run:
        checks.append(
            GateCheck(
                "current_conservation",
                Status.FAIL,
                "the currents into the package pins do not sum to zero, so some current "
                f"returns through the simulator's ground instead of a pin ({worst_run})",
                {"worst_residual_a": worst_kcl},
            )
        )
    else:
        checks.append(
            GateCheck(
                "current_conservation",
                Status.PASS,
                f"currents into the pins sum to zero in all {len(good)} benches "
                f"(ground pin held {OFFSET_V:g} V above node 0)",
            )
        )

    # c. outputs are current limited
    shorted = [r for r in good if r.run.short != "none"]
    if not outputs:
        checks.append(GateCheck("output_short_limited", Status.NOT_APPLICABLE, "no output pin"))
    elif not shorted:
        checks.append(GateCheck("output_short_limited", Status.UNKNOWN, "no short bench gave data"))
    else:
        peak = {p: 0.0 for p in outputs}
        where = {p: "" for p in outputs}
        for r in shorted:
            for p in outputs:
                if abs(r.currents.get(p, 0.0)) > peak[p]:
                    peak[p], where[p] = abs(r.currents[p]), r.run.name
        over, unrated = [], []
        for p in outputs:
            rating = spec.pin(p).isc_max if spec.pin(p) is not None else None
            if rating is not None:
                if peak[p] > SHORT_LIMIT_FACTOR * rating:
                    over.append(f"{p} carried {peak[p]:.4g} A in {where[p]} (rating {rating:g} A)")
            elif peak[p] > GENERIC_SHORT_LIMIT_A:
                over.append(f"{p} carried {peak[p]:.4g} A in {where[p]} (no cited rating)")
            else:
                unrated.append(p)
        measured = {f"peak_a_{p}": v for p, v in peak.items()}
        if over:
            checks.append(
                GateCheck(
                    "output_short_limited",
                    Status.FAIL,
                    "an output has no realistic current limit: " + "; ".join(over),
                    measured,
                )
            )
        elif unrated:
            checks.append(
                GateCheck(
                    "output_short_limited",
                    Status.UNKNOWN,
                    f"outputs {unrated} stayed under {GENERIC_SHORT_LIMIT_A:g} A but the pin table "
                    "cites no short-circuit rating to judge them against",
                    measured,
                )
            )
        else:
            checks.append(
                GateCheck(
                    "output_short_limited",
                    Status.PASS,
                    "every output stayed within "
                    f"{SHORT_LIMIT_FACTOR:g}x its cited short-circuit rating",
                    measured,
                )
            )

    # d. the supply pins deliver the current the outputs deliver
    worst_gap, gap_text, exercised = 0.0, "", False
    for r in shorted:
        sourced = sum(max(0.0, -r.currents.get(p, 0.0)) for p in outputs)
        sunk = sum(max(0.0, r.currents.get(p, 0.0)) for p in outputs)
        if sourced > ACTIVE_OUTPUT_A:
            exercised = True
            drawn = sum(r.currents.get(p, 0.0) for p in supplies)
            gap = SUPPLY_COVER * sourced - SUPPLY_COVER_FLOOR_A - drawn
            if gap > worst_gap:
                worst_gap = gap
                gap_text = (
                    f"{r.run.name}: outputs sourced {sourced:.4g} A but the supply pins "
                    f"supplied {drawn:.4g} A"
                )
        if sunk > ACTIVE_OUTPUT_A:
            exercised = True
            returned = -sum(r.currents.get(p, 0.0) for p in grounds)
            gap = SUPPLY_COVER * sunk - SUPPLY_COVER_FLOOR_A - returned
            if gap > worst_gap:
                worst_gap = gap
                gap_text = (
                    f"{r.run.name}: outputs sank {sunk:.4g} A but the ground pins "
                    f"returned {returned:.4g} A"
                )
    if not outputs:
        checks.append(
            GateCheck("supply_carries_output_current", Status.NOT_APPLICABLE, "no output pin")
        )
    elif gap_text:
        checks.append(
            GateCheck(
                "supply_carries_output_current",
                Status.FAIL,
                "output current is not drawn through the supply/ground pins: " + gap_text,
                {"worst_gap_a": worst_gap},
            )
        )
    elif exercised:
        checks.append(
            GateCheck(
                "supply_carries_output_current",
                Status.PASS,
                "whenever an output delivered current, the supply and ground pins carried it",
            )
        )
    else:
        checks.append(
            GateCheck(
                "supply_carries_output_current",
                Status.UNKNOWN,
                f"no bench made an output deliver more than {ACTIVE_OUTPUT_A:g} A",
            )
        )

    # e. floating inputs stay inside the rails
    floating = next((r for r in good if r.run.name == "float"), None)
    driven = [p for p in ports if kinds[p] in ("input", "io", "analog")]
    if floating is None or not driven:
        checks.append(GateCheck("floating_inputs_in_rails", Status.UNKNOWN, "no floating bench"))
    else:
        v_gnd = next((floating.volts[p] for p in grounds if p in floating.volts), OFFSET_V)
        v_top = max((floating.volts[p] for p in supplies if p in floating.volts), default=None)
        out_of_range = []
        for p in driven:
            v = floating.volts.get(p)
            if v is None:
                continue
            low_bad = v < v_gnd - RAIL_MARGIN_V
            high_bad = v_top is not None and v > v_top + RAIL_MARGIN_V
            if low_bad or high_bad:
                out_of_range.append(f"{p} floats at {v - v_gnd:.3g} V above the ground pin")
        rail = None if v_top is None else v_top - v_gnd
        checks.append(
            GateCheck(
                "floating_inputs_in_rails",
                Status.FAIL if out_of_range else Status.PASS,
                (
                    "a floating input rises outside the supply rails (no clamp or default): "
                    + "; ".join(out_of_range)
                    if out_of_range
                    else "every floating input settles inside the rails"
                ),
                {"rail_v": rail if rail is not None else float("nan")},
            )
        )

    # f. the supply pins draw a quiescent current, and nc pins draw nothing
    quiet = next((r for r in good if r.run.name == "low"), None)
    if quiet is None or not supplies:
        checks.append(GateCheck("supply_draws_current", Status.UNKNOWN, "no low-input bench"))
    else:
        drawn = sum(quiet.currents.get(p, 0.0) for p in supplies)
        checks.append(
            GateCheck(
                "supply_draws_current",
                Status.PASS if drawn > MIN_SUPPLY_A else Status.FAIL,
                f"supply pins draw {drawn:.4g} A with the inputs grounded"
                + (
                    ""
                    if drawn > MIN_SUPPLY_A
                    else " (a supply that draws nothing cannot load a rail)"
                ),
                {"supply_a": drawn},
            )
        )
    if nc_pins:
        stray = [
            f"{p} carried {r.currents[p]:.3g} A in {r.run.name}"
            for r in good
            for p in nc_pins
            if abs(r.currents.get(p, 0.0)) > 1e-9
        ]
        checks.append(
            GateCheck(
                "nc_pins_inert",
                Status.FAIL if stray else Status.PASS,
                "; ".join(stray[:2]) if stray else "no-connect pins carry no current",
            )
        )
    checks.extend(_alarm_checks(spec, ports, good))
    return checks, len(results)


def _alarm_checks(spec: GateSpec, ports: Sequence[str], good: Sequence[_Result]) -> list[GateCheck]:
    """Each claimed alarm must be in the model, quiet in clean use and firing on its fault."""
    claimed = _claimed_alarms(spec, ports)
    if not claimed:
        return []
    checks: list[GateCheck] = []
    absent = [node for _kind, _port, node in claimed if not any(node in r.alarms for r in good)]
    clean = [r for r in good if r.run.name in ("low", "high")]
    loud = [
        f"{node} read {level:.2f} in the {r.run.name} bench"
        for r in clean
        for node, level in r.alarms.items()
        if level > ALARM_QUIET
    ]
    if absent:
        status, detail = Status.FAIL, f"alarms the pin table claims are not in the model: {absent}"
    elif not clean:
        status, detail = Status.UNKNOWN, "no clean bench gave data"
    elif loud:
        status, detail = Status.FAIL, "an alarm fires in clean use: " + "; ".join(loud[:3])
    else:
        status = Status.PASS
        detail = (
            f"all {len(claimed)} alarms stay quiet with valid inputs, every required pin tied "
            "and nothing shorted"
        )
    checks.append(
        GateCheck("alarms_quiet_when_clean", status, detail, {"alarms": float(len(claimed))})
    )

    for kind in ALARM_KINDS:
        entries = [(port, node) for k, port, node in claimed if k == kind]
        if not entries:
            continue
        silent, unrun = [], []
        for port, node in entries:
            if kind == "abs":
                pool = [r for r in good if r.run.name == f"over_{port}"]
            elif kind == "flt":
                pool = [r for r in good if r.run.name == "float"]
            elif kind == "tie":
                pool = [r for r in good if r.run.name == "tie_open"]
            else:
                pool = [r for r in good if r.run.short != "none"]
            if not pool:
                unrun.append(node)
                continue
            peak = max(r.alarms.get(node, 0.0) for r in pool)
            if peak < ALARM_FIRES:
                silent.append(f"{node} peaked at {peak:.2f}")
        check_id = f"alarm_{kind}_fires_on_fault"
        if silent:
            checks.append(
                GateCheck(check_id, Status.FAIL, "the fault does not trip it: " + "; ".join(silent))
            )
        elif unrun:
            checks.append(
                GateCheck(check_id, Status.UNKNOWN, f"no fault bench gave data for {unrun}")
            )
        else:
            checks.append(
                GateCheck(
                    check_id,
                    Status.PASS,
                    f"{len(entries)} alarm(s) fire when their fault is applied",
                )
            )
    return checks


def run_gate(
    lib_path: Path,
    spec: GateSpec,
    ltspice: Path,
    workdir: Path,
    *,
    asy_path: Path | None = None,
    dynamic: bool = True,
) -> GateReport:
    """Run every check; the report status is FAIL if any check fails.

    A report whose dynamic benches did not run is never PASS: the benches are the
    evidence, and a static-only report says UNKNOWN for them.
    """
    started = time.monotonic()
    lib_path = Path(lib_path).resolve()
    text = lib_path.read_text(encoding="utf-8", errors="replace")
    asy_text = Path(asy_path).read_text(encoding="utf-8") if asy_path is not None else None
    checks = static_checks(text, spec, asy_text=asy_text)
    runs = 0
    if not dynamic:
        checks.append(
            GateCheck("dynamic_benches", Status.UNKNOWN, "the dynamic benches were not run")
        )
    elif not Path(ltspice).is_file():
        checks.append(
            GateCheck("dynamic_benches", Status.BLOCKED, f"LTspice not found at {ltspice}")
        )
    elif not _ports_or_empty(text, spec):
        checks.append(
            GateCheck("dynamic_benches", Status.UNKNOWN, "the subcircuit has no usable ports")
        )
    else:
        found, runs = dynamic_checks(lib_path, spec, Path(ltspice), Path(workdir))
        checks.extend(found)
    return GateReport(spec.part, checks, runs, time.monotonic() - started)


def _ports_or_empty(text: str, spec: GateSpec) -> tuple[str, ...]:
    try:
        return subckt_ports(text, spec.subckt)
    except Exception:
        return ()
