"""Which parts the engine may claim to support, on which route, and why.

A part is *supported* only when all three hold:

1. a behavioural implementation positively matches it (never a default or a guess);
2. every cited input that implementation needs is cited, not filled by a template default;
3. independent functional tests exist for its essential behaviours: rows bound to a probe
   whose limits come from the datasheet, not from the candidate.

Anything else is not supported, and an unknown class is never treated as supported.
Devices the engine cannot represent at all (microcontrollers, FPGAs, CPLDs, SoCs and
processors) are blocked on every route, legacy AI and pin-only included. Pin-only output
is a separately requested, limited mode for eligible components; it is never what a
missing behavioural implementation falls back to, and neither is AI authoring.

This module decides; it builds nothing and judges nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from boardmodeler.authoring.part_class import classify
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.models import op_amp
from boardmodeler.models.buck_switching import (
    BuckDesign,
    TemplateSeedError,
    design_from_spec,
    seed_from_spec,
)

__all__ = [
    "FAMILY_SIGNALS",
    "IMPLEMENTATIONS",
    "ORDINARY_FAMILIES",
    "ROUTES",
    "Implementation",
    "Route",
    "RouteVerdict",
    "State",
    "SupportDecision",
    "decide_support",
    "family_ids",
    "identify_family",
    "identify_family_scored",
]

Route = Literal["behavioral", "pin_only", "legacy_ai"]
State = Literal["supported", "blocked_class", "unsupported_family", "unclassified"]
ROUTES: tuple[Route, ...] = ("behavioral", "pin_only", "legacy_ai")

#: Ordinary component families a part may be identified as, from the system-model catalog.
#: Each row is (id, label, phrases); a phrase is a whole word or words in the part string or
#: the datasheet title, with an optional plural. Microcontrollers, FPGAs and their kin are
#: deliberately absent: they are blocked before this table is read.
ORDINARY_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "switching_regulator",
        "switching regulator",
        (
            "buck converter",
            "step-down converter",
            "step-down regulator",
            "step-up converter",
            "boost converter",
            "buck-boost",
            "switching regulator",
            "dc-dc converter",
            "dc/dc converter",
            "synchronous buck",
            "sepic",
            "pwm controller",
            "switching controller",
        ),
    ),
    (
        "linear_regulator",
        "linear regulator",
        ("ldo", "low-dropout", "low dropout", "linear regulator", "voltage regulator"),
    ),
    (
        "isolated_power",
        "isolated power",
        ("isolated dc-dc", "isolated power", "transformer driver"),
    ),
    ("charge_pump_pmic", "charge pump or PMIC", ("charge pump", "pmic", "power management ic")),
    (
        "power_path",
        "power-path switch",
        (
            "load switch",
            "power switch",
            "efuse",
            "e-fuse",
            "hot-swap",
            "hot swap",
            "ideal diode",
            "power multiplexer",
            "surge stopper",
        ),
    ),
    (
        "supervisor",
        "supervisor or sequencer",
        (
            "supervisor",
            "supervisory",
            "reset ic",
            "voltage detector",
            "watchdog",
            "power-good",
            "sequencer",
        ),
    ),
    (
        "reference_monitor",
        "reference or monitor",
        (
            "voltage reference",
            "shunt reference",
            "precision reference",
            "current-sense amplifier",
            "current sense amplifier",
            "power monitor",
            "voltage monitor",
        ),
    ),
    (
        "amplifier_comparator",
        "amplifier or comparator",
        (
            "operational amplifier",
            "op amp",
            "op-amp",
            "comparator",
            "instrumentation amplifier",
            "difference amplifier",
            "differential amplifier",
            "programmable gain amplifier",
            "transimpedance",
        ),
    ),
    (
        "data_converter",
        "data converter",
        (
            "analog-to-digital",
            "digital-to-analog",
            "adc",
            "dac",
            "data converter",
            "analog front end",
        ),
    ),
    (
        "analog_routing",
        "analog switch or mux",
        (
            "analog switch",
            "analog multiplexer",
            "multiplexer",
            "demultiplexer",
            "crosspoint",
            "digital potentiometer",
        ),
    ),
    (
        "clock_timing",
        "clock or timing",
        (
            "oscillator",
            "crystal",
            "clock buffer",
            "clock generator",
            "pll",
            "real-time clock",
            "timer",
        ),
    ),
    (
        "digital_glue",
        "digital glue logic",
        (
            "logic gate",
            "nand gate",
            "nor gate",
            "and gate",
            "or gate",
            "inverter",
            "level translator",
            "level shifter",
            "voltage translator",
            "bus switch",
            "schmitt",
            "flip-flop",
            "latch",
            "shift register",
            "i/o expander",
        ),
    ),
    (
        "memory_id",
        "memory or ID",
        ("eeprom", "fram", "flash memory", "serial flash", "sram", "1-wire"),
    ),
    (
        "wired_interface",
        "wired interface",
        (
            "rs-485",
            "rs485",
            "rs-232",
            "rs-422",
            "can transceiver",
            "lin transceiver",
            "lvds",
            "i2c buffer",
            "i2c switch",
        ),
    ),
    (
        "isolation",
        "isolation",
        ("digital isolator", "isolator", "optocoupler", "opto-coupler", "photocoupler"),
    ),
    (
        "load_driver",
        "output or load driver",
        (
            "gate driver",
            "motor driver",
            "led driver",
            "relay driver",
            "half-bridge driver",
            "h-bridge",
            "high-side switch",
            "low-side switch",
            "solenoid driver",
        ),
    ),
    (
        "protection",
        "connector-side protection",
        ("tvs", "esd protection", "varistor", "common-mode choke", "reverse polarity"),
    ),
    (
        "discrete_semiconductor",
        "discrete semiconductor",
        ("mosfet", "transistor", "bjt", "jfet", "diode", "rectifier", "solid-state relay"),
    ),
    ("passive", "passive or interconnect", ("resistor", "capacitor", "inductor", "connector")),
    (
        "sensor_load",
        "sensor or field load",
        ("temperature sensor", "hall sensor", "hall-effect", "thermistor", "current sensor"),
    ),
)


#: Phrases of the electrical-characteristics rows that mostly one kind of part has. They are
#: read from the cited row statements only, and never on their own name a device: a family
#: is read from rows only when two distinct signals of it appear and no other family has as
#: many. Generic words (resistor, capacitor, timer, latch) are deliberately not signals.
FAMILY_SIGNALS: dict[str, tuple[str, ...]] = {
    "switching_regulator": (
        "switching frequency",
        "oscillator frequency",
        "high-side",
        "current limit",
        "soft-start",
        "soft start",
        "minimum on-time",
        "maximum duty",
        "peak current",
        "feedback voltage",
        "error amplifier",
        "transconductance",
    ),
    "linear_regulator": (
        "dropout voltage",
        "line regulation",
        "load regulation",
        "output noise",
        "power supply rejection",
        "psrr",
    ),
    "charge_pump_pmic": (
        "voltage conversion efficiency",
        "power efficiency",
        "output source resistance",
        "flying capacitor",
        "output resistance",
    ),
    "supervisor": (
        "reset threshold",
        "reset timeout",
        "reset delay",
        "threshold voltage",
        "watchdog timeout",
        "power-good threshold",
    ),
    "reference_monitor": (
        "reference voltage",
        "cathode current",
        "reference input current",
        "temperature coefficient",
        "initial accuracy",
    ),
    "amplifier_comparator": (
        "input offset voltage",
        "input bias current",
        "input offset current",
        "gain bandwidth",
        "slew rate",
        "common-mode rejection",
        "open-loop gain",
        "large-signal",
        "unity-gain",
        "input common-mode",
        "output voltage swing",
    ),
    "data_converter": (
        "integral nonlinearity",
        "differential nonlinearity",
        "signal-to-noise",
        "effective number of bits",
        "sampling rate",
        "conversion time",
    ),
    "analog_routing": ("charge injection", "off isolation", "crosstalk", "on-resistance flatness"),
    "clock_timing": ("output frequency", "frequency stability", "jitter", "phase noise"),
    "digital_glue": (
        "propagation delay",
        "high-level output voltage",
        "low-level output voltage",
        "high-level input voltage",
        "low-level input voltage",
        "input capacitance",
    ),
    "memory_id": ("write cycle time", "endurance", "data retention", "page write"),
    "wired_interface": (
        "differential output voltage",
        "receiver input threshold",
        "driver output",
        "dominant",
        "recessive",
    ),
    "isolation": (
        "isolation voltage",
        "common-mode transient immunity",
        "creepage",
        "working voltage",
    ),
    "load_driver": ("gate drive", "peak output current", "dead time", "bootstrap"),
    "power_path": ("turn-on delay", "reverse leakage", "thermal shutdown", "fault flag"),
    "protection": ("clamping voltage", "breakdown voltage", "standoff voltage", "peak pulse"),
    "discrete_semiconductor": (
        "drain-source",
        "gate threshold",
        "forward voltage",
        "reverse recovery",
        "collector-emitter",
    ),
}


def family_ids() -> tuple[str, ...]:
    """The ids an operator may declare."""
    return tuple(family_id for family_id, _label, _phrases in ORDINARY_FAMILIES)


@dataclass(frozen=True)
class RouteVerdict:
    """Whether one generation route may run for a part, and the sentence that says why."""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class SupportDecision:
    """What the engine may claim about one part, and which routes may run for it."""

    part: str
    state: State
    family: str | None
    implementation: str | None
    reason: str
    missing: tuple[str, ...]
    routes: Mapping[Route, RouteVerdict] = field(default_factory=dict)
    identified_from: str = ""

    @property
    def supported(self) -> bool:
        return self.state == "supported"

    def allows(self, route: Route) -> bool:
        return self.routes[route].allowed

    def refusal(self, route: Route) -> str:
        """The BLOCKED detail for a refused route: precise, and never a hidden fallback."""
        verdict = self.routes[route]
        if verdict.allowed:
            raise ValueError(f"the {route} route is allowed for {self.part}")
        return verdict.reason


def _routes(state: State, reason: str) -> dict[Route, RouteVerdict]:
    if state == "blocked_class":
        refused = RouteVerdict(False, reason)
        return {route: refused for route in ROUTES}
    if state == "unclassified":
        refused = RouteVerdict(False, f"unsupported_part: unclassified: {reason}")
        return {route: refused for route in ROUTES}
    pin_only = RouteVerdict(True, "limited: pins only, no function; needs a confirmed pin table")
    legacy = RouteVerdict(
        True, "explicit legacy AI authoring; results are measured but never declared supported"
    )
    if state == "supported":
        return {
            "behavioral": RouteVerdict(True, reason),
            "pin_only": pin_only,
            "legacy_ai": legacy,
        }
    return {
        "behavioral": RouteVerdict(False, f"unsupported_part: unsupported_family: {reason}"),
        "pin_only": pin_only,
        "legacy_ai": legacy,
    }


def _mentions(text: str, phrase: str) -> bool:
    return re.search("(^|[^a-z0-9])" + re.escape(phrase) + "s?($|[^a-z0-9])", text) is not None


def identify_family(text: str) -> tuple[str, str] | None:
    """The first ordinary family the text names, as (id, label), else None."""
    normalized = " ".join(text.split()).casefold()
    for family_id, label, phrases in ORDINARY_FAMILIES:
        if any(_mentions(normalized, phrase) for phrase in phrases):
            return family_id, label
    return None


def _hits(text: str, phrases: Sequence[str]) -> int:
    return sum(1 for phrase in phrases if _mentions(text, phrase))


def identify_family_scored(
    *, part: str = "", title: str = "", head: str = "", statements: Sequence[str] = ()
) -> tuple[str, str, str] | None:
    """The ordinary family the evidence names, as (id, label, where it was read), else None.

    Sources, strongest first: the part number and datasheet title (6 points per phrase), the
    head of the datasheet first page (2 points per phrase, at most 4) and the family signals
    in the cited rows (1 point each, at most 4, and only when at least two distinct signals
    of that family appear, since a row can mention anything). So a title always outweighs
    what a features list happens to mention. The family with the most points wins; a tie
    goes to the earlier family in the table. Below 2 points nothing is identified.
    """
    named = " ".join(f"{part} {title}".split()).casefold()
    front = " ".join(head.split()).casefold()
    rows = " ".join(" ".join(statements).split()).casefold()
    best: tuple[int, int, str, str, str] | None = None
    for order, (family_id, label, phrases) in enumerate(ORDINARY_FAMILIES):
        by_name, by_front = _hits(named, phrases), _hits(front, phrases)
        by_rows = _hits(rows, FAMILY_SIGNALS.get(family_id, ()))
        points = 6 * by_name + 2 * min(by_front, 2) + (min(by_rows, 4) if by_rows >= 2 else 0)
        if points < 2:
            continue
        if by_name:
            where = "the part number or datasheet title"
        elif by_front:
            where = "the first page of the datasheet"
        else:
            where = "the cited rows"
        if best is None or (points, -order) > (best[0], best[1]):
            best = (points, -order, family_id, label, where)
    if best is None:
        return None
    return best[2], best[3], best[4]


@dataclass(frozen=True)
class Implementation:
    """A behavioural implementation and the three questions that make it a support claim.

    matches returns the implementation own design object for a spec it positively
    recognises, else None. uncited names the essential inputs that design took from a
    template default rather than a cited row. untested names the essential behaviours no
    bound (independent) row covers.
    """

    name: str
    family: str
    label: str
    matches: Callable[[SpecSet, Collection[str]], object | None]
    uncited: Callable[[object], tuple[str, ...]]
    untested: Callable[[SpecSet], tuple[str, ...]]
    #: Builds the starting candidate the behavioural route judges: an object with write(path),
    #: payload() and design (whose record(delivered_bytes) ties it to the delivered file).
    #: Raises ValueError for a spec it cannot render faithfully.
    seed: Callable[[SpecSet, Collection[str]], Any] | None = None
    #: Provenance names: the file and route label, and the heading on the model card.
    template: str = ""
    heading: str = ""


# Inputs without which the first-order behaviour of the buck would be a template default.
_BUCK_INPUTS = ("VREF", "FSW", "ILIM", "GMCS", "ISS", "ENTH", "UVTH")
# Essential behaviours, each needing at least one bound row whose statement names it.
_BUCK_BEHAVIOURS = (
    ("reference voltage", "reference|feedback|output voltage"),
    ("switching frequency", "switching frequency|oscillator frequency"),
    ("current limit", "current limit"),
    ("soft start", "soft.?start|slow.?start"),
    ("enable and undervoltage lockout", "enable|undervoltage|uvlo"),
)


def _match_buck(spec: SpecSet, unverified: Collection[str]) -> object | None:
    try:
        return design_from_spec(spec, unverified=unverified)
    except TemplateSeedError:
        return None


def _buck_uncited(design: object) -> tuple[str, ...]:
    assert isinstance(design, BuckDesign)
    origin = {item.name: item.origin for item in design.parameters}
    return tuple(name for name in _BUCK_INPUTS if origin[name] == "template_default")


def _bound_gaps(spec: SpecSet, behaviours: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    bound = [row.statement for row in spec.characteristics if row.probe]
    return tuple(
        label
        for label, pattern in behaviours
        if not any(re.search(pattern, statement, re.I) for statement in bound)
    )


def _match_op_amp(spec: SpecSet, unverified: Collection[str]) -> object | None:
    try:
        return op_amp.design_from_spec(spec, unverified=unverified)
    except op_amp.OpAmpDesignError:
        return None


def _op_amp_uncited(design: object) -> tuple[str, ...]:
    assert isinstance(design, op_amp.OpAmpDesign)
    origin = {item.name: item.origin for item in design.parameters}
    return tuple(name for name in op_amp.ESSENTIAL_INPUTS if origin[name] == "template_default")


def _op_amp_untested(spec: SpecSet) -> tuple[str, ...]:
    bound = {row.probe for row in spec.characteristics if row.probe}
    return tuple(
        label for label, probes in op_amp.BEHAVIOURS if not all(probe in bound for probe in probes)
    )


# The behavioural implementations registered so far, one per family that has independent tests.
IMPLEMENTATIONS: tuple[Implementation, ...] = (
    Implementation(
        name="peak_current_buck",
        family="switching_regulator",
        label="peak-current buck converter",
        matches=_match_buck,
        uncited=_buck_uncited,
        untested=lambda spec: _bound_gaps(spec, _BUCK_BEHAVIOURS),
        seed=lambda spec, unverified: seed_from_spec(spec, unverified=unverified),
        template="buck_template",
        heading="Buck template",
    ),
    Implementation(
        name="dual_op_amp",
        family="amplifier_comparator",
        label="dual op amp",
        matches=_match_op_amp,
        uncited=_op_amp_uncited,
        untested=_op_amp_untested,
        seed=lambda spec, unverified: op_amp.seed_from_spec(spec, unverified=unverified),
        template="op_amp_template",
        heading="Op amp template",
    ),
)


def decide_support(
    part: str,
    *,
    title: str = "",
    head: str = "",
    spec: SpecSet | None = None,
    unverified: Collection[str] = (),
    registry: tuple[Implementation, ...] = IMPLEMENTATIONS,
    declared_family: str | None = None,
) -> SupportDecision:
    """Decide what may be claimed for a part and which routes may run.

    Blocked classes come first and stop every route. A part is supported only through a
    positively matching implementation whose essential inputs are cited and whose essential
    behaviours have independent bound tests. A part nothing identifies is unclassified and
    is refused on every route; a part identified as an ordinary family without an
    implementation is unsupported_family, open only to the explicit limited routes.
    The operator may declare the family (declared_family) when the evidence names none; that
    can never unblock a class, and it never makes a part supported.
    """
    blocked = classify(part, text=title)
    if not blocked.supported:
        state: State = "blocked_class"
        return SupportDecision(
            part, state, blocked.kind, None, blocked.detail, (), _routes(state, blocked.detail)
        )
    if declared_family is not None and declared_family not in family_ids():
        raise ValueError(f"family must be one of {family_ids()}, got {declared_family!r}")
    if spec is not None:
        for impl in registry:
            design = impl.matches(spec, unverified)
            if design is None:
                continue
            missing = tuple(f"cited input {name}" for name in impl.uncited(design)) + tuple(
                f"independent test for {behaviour}" for behaviour in impl.untested(spec)
            )
            if missing:
                reason = (
                    f"the {impl.label} implementation matches, but a supported claim still "
                    "lacks: " + "; ".join(missing)
                )
                state = "unsupported_family"
            else:
                reason = (
                    f"the {impl.label} implementation matches, every essential input is cited "
                    "and every essential behaviour has an independent bound test"
                )
                state = "supported"
            return SupportDecision(
                part,
                state,
                impl.family,
                impl.name,
                reason,
                missing,
                _routes(state, reason),
                "the cited rows",
            )
    statements = () if spec is None else tuple(row.statement for row in spec.characteristics[:60])
    found = identify_family_scored(part=part, title=title, head=head, statements=statements)
    if declared_family is not None:
        family_id = declared_family
        label = next(text for fid, text, _ in ORDINARY_FAMILIES if fid == declared_family)
        where = "declared by the operator"
        if found is not None and found[0] != declared_family:
            where += f" (the evidence reads as {found[1]})"
    elif found is not None:
        family_id, label, where = found
    else:
        reason = (
            "nothing identifies the part as an ordinary component family: its number, "
            "datasheet title and first page name none, and no behavioural implementation "
            "matches; say what it is with the family option (" + ", ".join(family_ids()) + ")"
        )
        state = "unclassified"
        return SupportDecision(
            part, state, None, None, reason, ("a family",), _routes(state, reason)
        )
    reason = f"the part reads as a {label}, and no behavioural implementation is registered for it"
    state = "unsupported_family"
    return SupportDecision(
        part,
        state,
        family_id,
        None,
        reason,
        ("a behavioural implementation",),
        _routes(state, reason),
        where,
    )
