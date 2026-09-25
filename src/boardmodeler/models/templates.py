"""Template-driven device families (Phase 6 step 1).

A *family* is a reduced behavioural topology emitted as LTspice ``.lib`` text.
A *part* in that family is data: a JSON contract under ``fixtures/templates/``
fills the family's parameter values, declares the behaviours the part claims,
and carries the application deck the regression baseline is produced with.
Adding a part in an existing family is a new JSON file, never a code edit.

Contract validation is strict because each of these mistakes would otherwise
produce a model that simulates the wrong thing; every one raises
:class:`ContractError` naming the offending field:

* an unknown parameter name (or a parameter the family does not declare),
* a wrong port count or a port order that differs from the emitted ``.subckt``
  line (checked against the rendered text, not just the family table),
* a claimed behaviour with no capability key,
* a missing required parameter,
* a ``supported`` claim for a capability key the family does not implement.

Capability honesty
------------------
:func:`capability_for` returns a :class:`~boardmodeler.domain.records.ModelCapability`
whose ``behaviors`` mapping carries exactly ``domain.records.BEHAVIOR_KEYS``.
The states are the family's *declared design scope*: a behaviour the family
does not implement is ``unsupported`` — never ``supported`` — and
``thermal_dependence`` is ``unsupported`` for every family here because none of
them models temperature.  The states are not a probe result; callers that have
run a capability probe must pass its test ids through ``probe_results``.

Families and their port order (fixed; a contract must restate it exactly):

========================== ========================================= ==========
Family                     Subcircuit                                Ports
========================== ========================================= ==========
``supervisor``             ``BM_SUPERVISOR``                         VIN PG GND
``load_switch``            ``BM_LOAD_SWITCH``                         VIN EN VOUT GND
``buck`` / ``ldo``         ``BM_REG_BUCK`` / ``BM_REG_LDO``          see ``models.regulator``
========================== ========================================= ==========

The ``buck`` and ``ldo`` renderers delegate to
:func:`boardmodeler.models.regulator.regulator_text` — their topology text is
not duplicated here.  ``supervisor`` and ``load_switch`` are emitted from the
templates in this module and reuse the probed primitives ``BM_SCHMITT`` and
``BM_DELAY`` verbatim.

Real-device qualification is deliberately not part of this module: a request to
qualify a real device is answered by
:func:`boardmodeler.models.regression.request_device_qualification`, which
returns BLOCKED with reason ``device_documentation_unavailable`` when no
(non-synthetic) device documentation is supplied.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boardmodeler.domain.enums import EvidenceLevel, ModelKind
from boardmodeler.domain.hashing import sha256_text
from boardmodeler.domain.records import BEHAVIOR_KEYS, Limit, ModelCapability
from boardmodeler.models.buck_switching import BuckSeed, seed_from_spec
from boardmodeler.models.primitives import primitive_text
from boardmodeler.models.regulator import (
    REGULATOR_EXTRA_PARAMS,
    REGULATOR_PARAMS,
    REGULATOR_PORT_ORDER,
    regulator_text,
)
from boardmodeler.simulation.deck import (
    DeckSpec,
    Include,
    MeasSpec,
    Source,
    TranSpec,
    write_deck,
)

__all__ = [
    "FAMILIES",
    "FAMILY_TEMPLATES",
    "ApplicationDeck",
    "BehaviorClaim",
    "BuckSeed",
    "ContractError",
    "DeckMeasure",
    "DeckSource",
    "FamilyDeck",
    "FamilyParameter",
    "FamilyTemplate",
    "SubcktHeader",
    "TemplateContract",
    "TemplateInstance",
    "TemplateParameter",
    "build_deck_spec",
    "capability_for",
    "instance_card",
    "load_contract",
    "parse_subckt_header",
    "render_subckt",
    "seed_from_spec",
    "validate_rendered_subckt",
    "write_application_deck",
]

FamilyName = Literal["supervisor", "ldo", "load_switch", "buck"]
ClaimStatus = Literal["supported", "unsupported", "not_tested"]
ParameterType = Literal["float", "int"]

FAMILIES: tuple[str, ...] = ("supervisor", "ldo", "load_switch", "buck")


class ContractError(ValueError):
    """A template contract is missing or inconsistent with its family."""


# --------------------------------------------------------------------------- #
# family tables (code authority for the interface)


@dataclass(frozen=True)
class FamilyParameter:
    """One parameter of a family's interface; ``default`` is the reference value."""

    name: str
    type: ParameterType
    unit: str
    default: float | int
    required: bool = True
    """A required parameter must be declared by the contract, not merely defaulted."""


@dataclass(frozen=True)
class FamilyTemplate:
    """A device family: fixed subcircuit, port order, parameters and renderer."""

    family: str
    subckt: str
    ports: tuple[str, ...]
    parameters: tuple[FamilyParameter, ...]
    supported_keys: frozenset[str]
    """Capability keys this family is allowed to declare ``supported``."""
    render: Callable[[TemplateContract], str]

    def parameter(self, name: str) -> FamilyParameter:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise ContractError(
            f"parameters: unknown parameter {name!r}; family {self.family!r} declares "
            f"{tuple(p.name for p in self.parameters)}"
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(parameter.name for parameter in self.parameters)


# --------------------------------------------------------------------------- #
# contract model (JSON shape)


class TemplateParameter(BaseModel):
    """A parameter value supplied by the contract."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParameterType
    unit: str
    default: float | int


class BehaviorClaim(BaseModel):
    """A behaviour the contract claims, mapped to exactly one capability key."""

    model_config = ConfigDict(extra="forbid")

    behavior: str
    capability: str
    status: ClaimStatus
    detail: str = ""


class TemplateInstance(BaseModel):
    """The instantiation used by the contract's application deck."""

    model_config = ConfigDict(extra="forbid")

    refdes: str
    nodes: dict[str, str]
    params: dict[str, float | int] = Field(default_factory=dict)


class DeckSource(BaseModel):
    """One independent source of the application deck."""

    model_config = ConfigDict(extra="forbid")

    name: str
    plus: str
    minus: str
    kind: Literal["dc", "ramp", "pulse", "pwl"]
    params: dict[str, float] = Field(default_factory=dict)
    points: list[tuple[float, float]] = Field(default_factory=list)


class DeckMeasure(BaseModel):
    """One measured value, emitted as a ``.meas`` card and read back from the ``.raw``.

    ``rel``/``abs`` are the regression tolerances for this value; when absent the
    comparison default (1 % relative) applies.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    signal: str
    kind: Literal["find_at", "avg", "max", "min", "pp"]
    at_s: float | None = None
    from_s: float | None = None
    to_s: float | None = None
    rel: float | None = None
    abs: float | None = None


class FamilyDeck(BaseModel):
    """The application circuit a baseline is produced from."""

    model_config = ConfigDict(extra="forbid")

    title: str
    tstep: float
    tstop: float
    tstart: float = 0.0
    tmax: float | None = None
    uic: bool = True
    instance: TemplateInstance
    elements: list[str] = Field(default_factory=list)
    sources: list[DeckSource]
    save: list[str] = Field(default_factory=list)
    measures: list[DeckMeasure]


class TemplateContract(BaseModel):
    """A concrete part in a family, filled from a JSON file."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    family: FamilyName
    part: str
    subckt: str
    ports: list[str]
    parameters: list[TemplateParameter]
    behaviors: list[BehaviorClaim]
    valid_domain: dict[str, Limit | str] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    notes: str = ""
    deck: FamilyDeck

    def parameter_values(self) -> dict[str, float | int]:
        """Every family parameter: the declared value, else the reference default."""
        declared = {parameter.name: parameter.default for parameter in self.parameters}
        return {
            parameter.name: declared.get(parameter.name, parameter.default)
            for parameter in FAMILY_TEMPLATES[self.family].parameters
        }


# --------------------------------------------------------------------------- #
# emitted text


_SUPERVISOR_BODY = Template(
    """\
* $SUBCKT (family supervisor): reduced behavioural supply supervisor
* ports: $PORTS
* declared scope: rail monitoring with hysteresis, PG assertion delay,
*   open-drain (sink-only) PG output, quiescent input current.
* not modelled: load regulation, switching, current limit, thermal behaviour.
* internal constants: comparator gain 1e4 V/V, RC lag 1 nF, sink gate 1e3 V/V.
B_vdd vdd GND V = 1
X_mon VIN GND mon_n vdd GND BM_SCHMITT VTH={(VTH_RISE+VTH_FALL)/2} VHYS={VTH_RISE-VTH_FALL} VOH=1 VOL=0 TPD=1u
B_monok monok GND V = 1-V(mon_n)
X_dly monok okd vdd GND BM_DELAY TD={PG_DELAY} TR=100n
B_sink PG GND I = limit(V(PG)/max(PG_SINK,1m), 0, 1)*limit((0.5-V(okd))*1e3, 0, 1)
B_iq VIN GND I = IQ*limit(V(VIN)/0.1, 0, 1)
.ends $SUBCKT"""
)

_LOAD_SWITCH_BODY = Template(
    """\
* $SUBCKT (family load_switch): reduced behavioural load switch
* ports: $PORTS
* declared scope: EN threshold with hysteresis and POL_EN polarity, capacitive
*   soft start, on-resistance, constant-current clamp, output discharge when
*   disabled, quiescent input current.
* not modelled: current-limit retry/latch recovery, reverse-current blocking,
*   switching behaviour, thermal behaviour.
* internal constants: ISS=1.7u, soft-start reset 100 ohm, charge stop 0.05 V,
*   discharge gate 0.01 V, EN comparator gain 1e4 V/V.
B_vdd vdd GND V = 1
X_en EN GND en_n vdd GND BM_SCHMITT VTH={(EN_RISE+EN_FALL)/2} VHYS={EN_RISE-EN_FALL} VOH=1 VOL=0 TPD=1u
B_enok enok GND V = POL_EN+(1-2*POL_EN)*V(en_n)
C_ss ss GND {CSS}
B_sschg 0 ss I = 1.7u*V(enok)*limit((1-V(ss))/0.05, 0, 1)
B_ssrst ss GND I = (1-V(enok))*V(ss)/100
R_sslk ss GND 1e10
B_pass VIN VOUT I = limit(V(enok)*V(ss)/max(RON,1m)*limit(V(VIN)-V(VOUT),0,1e3), 0, ILIM)
B_dis VOUT GND I = V(VOUT)/max(RDISCHARGE,10)*limit((1-V(enok))/0.01, 0, 1)
B_iq VIN GND I = IQ*limit(V(VIN)/0.1, 0, 1)
.ends $SUBCKT"""
)

_LIBRARY_HEADER = (
    "* BoardModeler template-family library\n"
    "* Emitted by boardmodeler.models.templates - do not edit by hand.\n"
    "* A deck includes this file by absolute path and instantiates the subckt\n"
    "* with the ports and parameter values printed on its .subckt line.\n"
)

_SPICE_SUFFIXES: dict[str, float] = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}
_SPICE_NUMBER_RE = re.compile(
    r"^(?P<number>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(?P<suffix>meg|[tgkmunpf])?$"
)

_SUBCKT_RE = re.compile(r"^\.subckt\s+(?P<name>\S+)\s*(?P<rest>.*?)\s*$")


def _parse_spice_number(token: str) -> float | None:
    """Parse a SPICE number (``10n``, ``8m``, ``1e-08``, ``3.3``); ``None`` if not one."""
    match = _SPICE_NUMBER_RE.match(token.strip().lower())
    if match is None:
        return None
    number = float(match.group("number"))
    suffix = match.group("suffix")
    return number * _SPICE_SUFFIXES[suffix] if suffix else number


def _fmt_spice(value: float | int) -> str:
    """Format a SPICE parameter value without losing precision or adding noise.

    Integer-valued numbers stay integral, a short decimal stays decimal, and a
    long decimal is written with its engineering suffix when that is shorter
    (mirrors the formatter in :mod:`boardmodeler.models.regulator`).
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise TypeError("boolean is not a valid SPICE value")
    number = float(value)
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e12:
        return str(int(number))
    plain = repr(number)
    if "e" not in plain and len(plain) <= 4:
        return plain
    for exponent, suffix in ((-3, "m"), (-6, "u"), (-9, "n"), (-12, "p"), (3, "k"), (6, "meg")):
        scaled = number / 10.0**exponent
        if 1.0 <= abs(scaled) < 1000.0 and abs(scaled * 1e6 - round(scaled * 1e6)) < 1e-6:
            candidate = f"{scaled:g}{suffix}"
            if len(candidate) < len(plain):
                return candidate
    return plain


def _subckt_line(contract: TemplateContract, values: Mapping[str, float | int]) -> str:
    names = FAMILY_TEMPLATES[contract.family].names
    declarations = " ".join(f"{name}={_fmt_spice(values[name])}" for name in names)
    return f".subckt {contract.subckt} {' '.join(contract.ports)} params: {declarations}"


def _library_text(contract: TemplateContract, body: str, primitives: Sequence[str]) -> str:
    values = contract.parameter_values()
    section = "\n\n".join(primitive_text(name) for name in primitives)
    return (
        f"{_LIBRARY_HEADER}\n{section}\n\n"
        f"{_subckt_line(contract, values)}\n"
        f"{body.substitute(SUBCKT=contract.subckt, PORTS=' '.join(contract.ports))}\n"
    )


def _render_supervisor(contract: TemplateContract) -> str:
    return _library_text(contract, _SUPERVISOR_BODY, ("BM_SCHMITT", "BM_DELAY"))


def _render_load_switch(contract: TemplateContract) -> str:
    return _library_text(contract, _LOAD_SWITCH_BODY, ("BM_SCHMITT", "BM_DELAY"))


def _render_regulator(kind: str) -> Callable[[TemplateContract], str]:
    def render(contract: TemplateContract) -> str:
        return regulator_text(kind, extra_params=contract.parameter_values())

    return render


_REGULATOR_SUPPORTED = frozenset(
    {
        "startup",
        "shutdown",
        "dc_regulation",
        "load_transients",
        "input_current",
        "current_limit_recovery",
        "reverse_current_prebias",
    }
)
"""Capability keys the regulator templates are allowed to declare ``supported``.

``compensation_loop`` is deliberately absent: a lag/zero compensation network
exists in the text, but no loop-gain probe is possible with the native reader,
so a generated regulator makes no loop claim.  ``switching_waveforms`` and
``thermal_dependence`` are absent because the templates do not drive ``SW`` and
model no temperature dependence.
"""

_SENSOR_SUPPORTED = frozenset({"startup", "shutdown", "input_current"})


def _float_parameter(
    name: str, unit: str, default: float, *, required: bool = True
) -> FamilyParameter:
    return FamilyParameter(name=name, type="float", unit=unit, default=default, required=required)


def _int_parameter(name: str, default: int, *, required: bool = True) -> FamilyParameter:
    return FamilyParameter(name=name, type="int", unit="", default=default, required=required)


def _regulator_parameters(kind: str) -> tuple[FamilyParameter, ...]:
    units: dict[str, str] = {
        "VREF": "V",
        "VOUT_NOM": "V",
        "UVLO_RISE": "V",
        "UVLO_FALL": "V",
        "EN_RISE": "V",
        "EN_FALL": "V",
        "POL_EN": "",
        "CSS": "F",
        "ILIM": "A",
        "ILIM_MODE": "",
        "RETRY_MS": "s",
        "RDISCHARGE": "ohm",
        "VPREBIAS_MAX": "V",
        "REVERSE_BLOCK": "",
        "ETA": "",
        "VMIN_FLOOR": "V",
        "IIN_MAX": "A",
        "GM": "S",
        "PG_DELAY": "s",
    }
    required = {"VREF", "VOUT_NOM", "UVLO_RISE", "UVLO_FALL"}
    ints = {"POL_EN", "ILIM_MODE", "REVERSE_BLOCK"}
    parameters: list[FamilyParameter] = []
    for name, default in {**REGULATOR_PARAMS[kind], **REGULATOR_EXTRA_PARAMS[kind]}.items():
        if name in ints:
            parameters.append(_int_parameter(name, int(default), required=name in required))
        else:
            parameters.append(
                _float_parameter(name, units[name], float(default), required=name in required)
            )
    return tuple(parameters)


FAMILY_TEMPLATES: dict[str, FamilyTemplate] = {
    "supervisor": FamilyTemplate(
        family="supervisor",
        subckt="BM_SUPERVISOR",
        ports=("VIN", "PG", "GND"),
        parameters=(
            _float_parameter("VTH_RISE", "V", 2.9),
            _float_parameter("VTH_FALL", "V", 2.8),
            _float_parameter("PG_SINK", "ohm", 50.0, required=False),
            _float_parameter("PG_DELAY", "s", 1e-3, required=False),
            _float_parameter("IQ", "A", 1e-5, required=False),
        ),
        supported_keys=_SENSOR_SUPPORTED,
        render=_render_supervisor,
    ),
    "load_switch": FamilyTemplate(
        family="load_switch",
        subckt="BM_LOAD_SWITCH",
        ports=("VIN", "EN", "VOUT", "GND"),
        parameters=(
            _float_parameter("RON", "ohm", 0.1),
            _float_parameter("ILIM", "A", 3.0),
            _float_parameter("EN_RISE", "V", 1.25, required=False),
            _float_parameter("EN_FALL", "V", 1.15, required=False),
            _int_parameter("POL_EN", 1, required=False),
            _float_parameter("CSS", "F", 1e-9, required=False),
            _float_parameter("RDISCHARGE", "ohm", 10.0, required=False),
            _float_parameter("IQ", "A", 1e-5, required=False),
        ),
        supported_keys=_SENSOR_SUPPORTED,
        render=_render_load_switch,
    ),
    "buck": FamilyTemplate(
        family="buck",
        subckt="BM_REG_BUCK",
        ports=REGULATOR_PORT_ORDER["BM_REG_BUCK"],
        parameters=_regulator_parameters("BM_REG_BUCK"),
        supported_keys=_REGULATOR_SUPPORTED,
        render=_render_regulator("BM_REG_BUCK"),
    ),
    "ldo": FamilyTemplate(
        family="ldo",
        subckt="BM_REG_LDO",
        ports=REGULATOR_PORT_ORDER["BM_REG_LDO"],
        parameters=_regulator_parameters("BM_REG_LDO"),
        supported_keys=_REGULATOR_SUPPORTED,
        render=_render_regulator("BM_REG_LDO"),
    ),
}


# --------------------------------------------------------------------------- #
# validation


def _validate_contract(contract: TemplateContract) -> None:
    template = FAMILY_TEMPLATES[contract.family]
    if contract.subckt != template.subckt:
        raise ContractError(
            f"subckt: family {contract.family!r} emits {template.subckt!r}, "
            f"the contract declares {contract.subckt!r}"
        )
    if tuple(contract.ports) != template.ports:
        raise ContractError(
            f"ports: family {contract.family!r} requires {template.ports} in that order, "
            f"the contract declares {tuple(contract.ports)} "
            f"({len(contract.ports)} port(s) vs {len(template.ports)})"
        )

    declared: dict[str, TemplateParameter] = {}
    for parameter in contract.parameters:
        if parameter.name in declared:
            raise ContractError(f"parameters[{parameter.name}]: declared twice")
        declared[parameter.name] = parameter
    unknown = [name for name in declared if name not in template.names]
    if unknown:
        raise ContractError(
            f"parameters: unknown parameter(s) {unknown}; "
            f"family {contract.family!r} declares {template.names}"
        )
    missing = [
        parameter.name
        for parameter in template.parameters
        if parameter.required and parameter.name not in declared
    ]
    if missing:
        raise ContractError(
            f"parameters: missing required parameter(s) {missing}; "
            f"family {contract.family!r} requires them in every contract"
        )
    for name, parameter in declared.items():
        spec = template.parameter(name)
        if parameter.type != spec.type:
            raise ContractError(
                f"parameters[{name}].type: {parameter.type!r} does not match the family's "
                f"{spec.type!r}"
            )
        if parameter.unit != spec.unit:
            raise ContractError(
                f"parameters[{name}].unit: {parameter.unit!r} does not match the family's "
                f"{spec.unit!r}"
            )
        if spec.type == "int" and not float(parameter.default).is_integer():
            raise ContractError(
                f"parameters[{name}].default: {parameter.default!r} is not an integer value"
            )

    claimed: dict[str, str] = {}
    for index, claim in enumerate(contract.behaviors):
        if claim.capability not in BEHAVIOR_KEYS:
            raise ContractError(
                f"behaviors[{index}].capability: {claim.capability!r} is not one of the "
                f"{len(BEHAVIOR_KEYS)} capability keys {BEHAVIOR_KEYS}"
            )
        previous = claimed.get(claim.capability)
        if previous is not None and previous != claim.status:
            raise ContractError(
                f"behaviors[{index}].status: capability {claim.capability!r} was already "
                f"claimed {previous!r}"
            )
        claimed[claim.capability] = claim.status
        if claim.status == "supported" and claim.capability not in template.supported_keys:
            raise ContractError(
                f"behaviors[{index}].capability: family {contract.family!r} does not implement "
                f"{claim.capability!r}, so it cannot be declared supported"
            )
        if claim.status == "unsupported" and claim.capability in template.supported_keys:
            raise ContractError(
                f"behaviors[{index}].status: family {contract.family!r} implements "
                f"{claim.capability!r}, so it cannot be declared unsupported"
            )

    _validate_parameter_relations(contract)
    _validate_deck(contract)


def _numeric(contract: TemplateContract, name: str) -> float:
    return float(contract.parameter_values()[name])


def _validate_parameter_relations(contract: TemplateContract) -> None:
    """Reject parameter combinations whose topology would be meaningless."""
    pairs: dict[str, tuple[str, str]] = {
        "supervisor": ("VTH_RISE", "VTH_FALL"),
        "load_switch": ("EN_RISE", "EN_FALL"),
        "buck": ("UVLO_RISE", "UVLO_FALL"),
        "ldo": ("UVLO_RISE", "UVLO_FALL"),
    }
    high, low = pairs[contract.family]
    if _numeric(contract, high) <= _numeric(contract, low):
        raise ContractError(
            f"parameters[{high}].default: must be greater than {low} "
            f"({_numeric(contract, high)!r} <= {_numeric(contract, low)!r})"
        )
    if contract.family == "load_switch":
        if _numeric(contract, "RON") <= 0:
            raise ContractError("parameters[RON].default: must be > 0")
        if _numeric(contract, "ILIM") <= 0:
            raise ContractError("parameters[ILIM].default: must be > 0")
        if _numeric(contract, "EN_RISE") <= _numeric(contract, "EN_FALL"):
            raise ContractError("parameters[EN_RISE].default: must be greater than EN_FALL")
    elif contract.family == "supervisor":
        if _numeric(contract, "PG_SINK") <= 0:
            raise ContractError("parameters[PG_SINK].default: must be > 0")
    elif _numeric(contract, "EN_RISE") <= _numeric(contract, "EN_FALL"):
        raise ContractError("parameters[EN_RISE].default: must be greater than EN_FALL")
    for name in ("CSS",):
        if name in contract.parameter_values() and _numeric(contract, name) <= 0:
            raise ContractError(f"parameters[{name}].default: must be > 0")


def _validate_deck(contract: TemplateContract) -> None:
    deck = contract.deck
    if deck.tstep <= 0 or deck.tstop <= 0:
        raise ContractError("deck.tstep/deck.tstop: both must be > 0")
    if deck.tstart < 0 or deck.tstart >= deck.tstop:
        raise ContractError("deck.tstart: must be >= 0 and < deck.tstop")
    if deck.tmax is not None and deck.tmax <= 0:
        raise ContractError("deck.tmax: must be > 0 when given")
    nodes = deck.instance.nodes
    missing = [port for port in contract.ports if port not in nodes]
    extra = [name for name in nodes if name not in contract.ports]
    if missing or extra:
        raise ContractError(
            f"deck.instance.nodes: must cover the ports {tuple(contract.ports)}; "
            f"missing {missing} and unexpected {extra}"
        )
    known = set(FAMILY_TEMPLATES[contract.family].names)
    unknown_params = [name for name in deck.instance.params if name not in known]
    if unknown_params:
        raise ContractError(
            f"deck.instance.params: unknown parameter(s) {unknown_params}; "
            f"family {contract.family!r} declares {tuple(sorted(known))}"
        )
    for index, source in enumerate(deck.sources):
        _validate_source(index, source)
    for index, measure in enumerate(deck.measures):
        _validate_measure(index, measure, deck)


_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "dc": ("v",),
    "ramp": ("v0", "v1"),
    "pulse": ("v1", "v2"),
    "pwl": (),
}


def _validate_source(index: int, source: DeckSource) -> None:
    missing = [key for key in _SOURCE_KEYS[source.kind] if key not in source.params]
    if missing:
        raise ContractError(f"deck.sources[{index}].params: kind {source.kind!r} needs {missing}")
    if source.kind == "pwl" and len(source.points) < 2:
        raise ContractError(f"deck.sources[{index}].points: a PWL source needs at least two points")
    if source.kind != "pwl" and source.points:
        raise ContractError(
            f"deck.sources[{index}].points: only a PWL source takes points, not {source.kind!r}"
        )


def _validate_measure(index: int, measure: DeckMeasure, deck: FamilyDeck) -> None:
    if measure.kind == "find_at":
        if measure.at_s is None:
            raise ContractError(f"deck.measures[{index}].at_s: find_at needs at_s")
        if not 0.0 <= measure.at_s <= deck.tstop:
            raise ContractError(
                f"deck.measures[{index}].at_s: {measure.at_s!r} is outside the run "
                f"[0, {deck.tstop}]"
            )
    else:
        if measure.from_s is None or measure.to_s is None:
            raise ContractError(f"deck.measures[{index}].from_s/to_s: {measure.kind} needs both")
        if not 0.0 <= measure.from_s < measure.to_s <= deck.tstop:
            raise ContractError(
                f"deck.measures[{index}].from_s/to_s: [{measure.from_s}, {measure.to_s}] is "
                f"not inside the run [0, {deck.tstop}]"
            )
    for name, tolerance in (("rel", measure.rel), ("abs", measure.abs)):
        if tolerance is not None and tolerance < 0:
            raise ContractError(f"deck.measures[{index}].{name}: must be >= 0")
    if not measure.signal:
        raise ContractError(f"deck.measures[{index}].signal: must not be empty")


# --------------------------------------------------------------------------- #
# rendered-text checks


@dataclass(frozen=True)
class SubcktHeader:
    """The parsed ``.subckt`` header of one emitted model."""

    name: str
    ports: tuple[str, ...]
    params: dict[str, str]


def parse_subckt_header(text: str, name: str) -> SubcktHeader | None:
    """The header of ``.subckt <name> ...`` in ``text``, or ``None`` when absent."""
    for line in text.splitlines():
        match = _SUBCKT_RE.match(line)
        if match is None or match.group("name") != name:
            continue
        rest = match.group("rest")
        marker = re.search(r"\bparams:\s*", rest, re.IGNORECASE)
        if marker is None:
            ports = rest.split()
            params: dict[str, str] = {}
        else:
            ports = rest[: marker.start()].split()
            params = {}
            for token in rest[marker.end() :].split():
                key, separator, value = token.partition("=")
                if not separator:
                    raise ContractError(
                        f"parameters: rendered .subckt {name} has a parameter token "
                        f"{token!r} without '='"
                    )
                params[key] = value
        return SubcktHeader(name=match.group("name"), ports=tuple(ports), params=params)
    return None


def validate_rendered_subckt(contract: TemplateContract, text: str) -> None:
    """Check the emitted ``.subckt`` line against the contract (drift guard)."""
    header = parse_subckt_header(text, contract.subckt)
    if header is None:
        raise ContractError(
            f"subckt: the rendered text contains no '.subckt {contract.subckt}' line"
        )
    if header.ports != tuple(contract.ports):
        raise ContractError(
            f"ports: rendered .subckt declares {header.ports}, the contract requires "
            f"{tuple(contract.ports)}"
        )
    expected = contract.parameter_values()
    missing = [name for name in expected if name not in header.params]
    if missing:
        raise ContractError(
            f"parameters: rendered .subckt is missing declared parameter(s) {missing}"
        )
    unknown = [name for name in header.params if name not in expected]
    if unknown:
        raise ContractError(f"parameters: rendered .subckt declares unknown parameter(s) {unknown}")
    for name, raw in header.params.items():
        parsed = _parse_spice_number(raw)
        if parsed is None:
            raise ContractError(f"parameters[{name}]: rendered value {raw!r} is not a SPICE number")
        if not math.isclose(parsed, float(expected[name]), rel_tol=1e-9, abs_tol=1e-15):
            raise ContractError(
                f"parameters[{name}].default: declared {expected[name]!r} but the rendered "
                f"text says {raw!r}"
            )


def render_subckt(contract: TemplateContract) -> str:
    """The self-contained emitted text: primitives, then the family ``.subckt``."""
    _validate_contract(contract)
    text = FAMILY_TEMPLATES[contract.family].render(contract)
    validate_rendered_subckt(contract, text)
    return text


# --------------------------------------------------------------------------- #
# capability


def _capability_states(contract: TemplateContract) -> dict[str, str]:
    rank = {"unsupported": 0, "not_tested": 1, "supported": 2}
    states = {key: "unsupported" for key in BEHAVIOR_KEYS}
    for claim in contract.behaviors:
        if rank[claim.status] > rank[states[claim.capability]]:
            states[claim.capability] = claim.status
    return states


def capability_for(
    contract: TemplateContract, *, probe_results: Sequence[str] = ()
) -> ModelCapability:
    """The contract's declared capability record.

    ``behaviors`` carries exactly ``BEHAVIOR_KEYS``; states are the *declared
    design scope*, not a probe result.  ``probe_results`` is the caller's place
    to attach the ids of capability probes that were actually run.
    """
    text = render_subckt(contract)
    return ModelCapability(
        model_id=contract.part,
        kind=ModelKind.REDUCED_BEHAVIORAL,
        behaviors=_capability_states(contract),
        valid_domain=dict(contract.valid_domain),
        exclusions=list(contract.exclusions),
        evidence_level=EvidenceLevel.SYNTHETIC_ANALYTICAL,
        probe_results=list(probe_results),
        source_model_hash=sha256_text(text),
    )


# --------------------------------------------------------------------------- #
# decks


def instance_card(
    contract: TemplateContract,
    refdes: str,
    nodes: Mapping[str, str],
    params: Mapping[str, float | int] | None = None,
) -> str:
    """One ``X`` instance card in the family's port order, with checked names."""
    missing = [port for port in contract.ports if port not in nodes]
    extra = [name for name in nodes if name not in contract.ports]
    if missing or extra:
        raise ContractError(
            f"nodes: {contract.subckt} ports are {tuple(contract.ports)}; "
            f"missing {missing} and unexpected {extra}"
        )
    known = FAMILY_TEMPLATES[contract.family].names
    values = dict(params or {})
    unknown = [name for name in values if name not in known]
    if unknown:
        raise ContractError(
            f"params: {contract.subckt} has no parameter(s) {unknown}; known: {known}"
        )
    fields = [refdes, *(str(nodes[port]) for port in contract.ports), contract.subckt]
    fields.extend(f"{name}={_fmt_spice(value)}" for name, value in values.items())
    return " ".join(fields)


def _source(spec: DeckSource) -> Source:
    params = spec.params
    if spec.kind == "dc":
        return Source.dc(spec.name, spec.plus, spec.minus, params["v"])
    if spec.kind == "ramp":
        return Source.ramp(
            spec.name,
            spec.plus,
            spec.minus,
            v0=params["v0"],
            v1=params["v1"],
            delay_s=params.get("delay_s", 0.0),
            rise_s=params.get("rise_s", 1e-3),
            hold_s=params.get("hold_s", 1.0),
        )
    if spec.kind == "pulse":
        return Source.pulse(
            spec.name,
            spec.plus,
            spec.minus,
            v1=params["v1"],
            v2=params["v2"],
            delay_s=params.get("delay_s", 0.0),
            rise_s=params.get("rise_s", 1e-6),
            fall_s=params.get("fall_s", 1e-6),
            width_s=params.get("width_s", 1e-3),
            period_s=params.get("period_s", 2e-3),
        )
    return Source(
        name=spec.name,
        terminals=(spec.plus, spec.minus),
        kind="pwl",
        points=tuple(tuple(point) for point in spec.points),
    )


def build_deck_spec(contract: TemplateContract, *, library: str | Path) -> DeckSpec:
    """The deck the contract describes, with ``library`` included by absolute path."""
    _validate_contract(contract)
    deck = contract.deck
    return DeckSpec(
        title=deck.title,
        includes=(Include(path=str(library)),),
        sources=tuple(_source(source) for source in deck.sources),
        elements=(
            *deck.elements,
            instance_card(
                contract, deck.instance.refdes, deck.instance.nodes, deck.instance.params
            ),
        ),
        tran=TranSpec(
            tstep=deck.tstep,
            tstop=deck.tstop,
            tstart=deck.tstart,
            tmax=deck.tmax,
            uic=deck.uic,
        ),
        save=tuple(deck.save),
        meas=tuple(
            MeasSpec(
                name=measure.name,
                signal=measure.signal,
                kind=measure.kind,
                at_s=measure.at_s,
                from_s=measure.from_s,
                to_s=measure.to_s,
            )
            for measure in deck.measures
        ),
    )


@dataclass(frozen=True)
class ApplicationDeck:
    """Where a contract's library and deck were written."""

    deck: Path
    library: Path


def write_application_deck(contract: TemplateContract, path: str | Path) -> ApplicationDeck:
    """Write the family library and the application deck; return both paths."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    library = target.parent / f"{contract.part}.lib"
    library.write_text(render_subckt(contract), encoding="utf-8", newline="\n")
    write_deck(build_deck_spec(contract, library=library), target)
    return ApplicationDeck(deck=target, library=library)


# --------------------------------------------------------------------------- #
# loading


def _format_validation_error(path: Path, error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        where = f"contract field {location}" if location else "contract"
        parts.append(f"{where}: {item['msg']}")
    shown = "; ".join(parts[:4])
    if len(parts) > 4:
        shown = f"{shown}; ... ({len(parts) - 4} more)"
    return f"{path}: {shown}"


def load_contract(path: str | Path) -> TemplateContract:
    """Parse and fully validate one contract JSON file.

    Raises :class:`ContractError` naming the offending field for any
    inconsistency with the family it belongs to.
    """
    target = Path(path)
    try:
        contract = TemplateContract.model_validate_json(target.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise ContractError(_format_validation_error(target, error)) from None
    _validate_contract(contract)
    return contract
