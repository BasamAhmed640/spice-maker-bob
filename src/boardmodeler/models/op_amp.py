"""A dual op amp built on the pin shell: a typed design, a pure renderer, cited inputs.

The second behavioural implementation of the engine, beside the peak-current buck. It only
recognises what its independent tests can cover: the eight-pin dual amplifier pinout the
op-amp probes in :mod:`boardmodeler.authoring.opamp_probes` drive, with at least one row
bound to an op-amp probe. Everything the model does comes from a cited row or is a labelled
default; the shell supplies the pins, the finite output stage and the supply accounting, and
this module supplies only the function core (offset, a transconductance stage into a
compensation node, an output level clamped to the rails).

A design holds no bench limit and no verdict. :func:`render_library` is a pure function of it
and of the renderer version. The LTspice harness, not this module, decides every row.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardmodeler.authoring.opamp_probes import PORTS as DUAL_PORTS
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.models.buck_switching import DESIGN_SCHEMA_VERSION, ParameterOrigin
from boardmodeler.models.pin_shell import ShellPin, render_shell

__all__ = [
    "BEHAVIOURS",
    "ESSENTIAL_INPUTS",
    "OP_AMP_RENDERER_VERSION",
    "OpAmpDesign",
    "OpAmpDesignError",
    "OpAmpSeed",
    "design_from_spec",
    "design_record_payload",
    "render_library",
    "seed_from_spec",
]

OP_AMP_RENDERER_VERSION = "dual_op_amp_render_v1"
_ORIGINS = ("cited_row", "derived_from_bounds", "template_default")
_CITED_ORIGINS = ("cited_row", "derived_from_bounds")
_SUBCKT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Fixed transconductance of the gain stage (S). It is a design constant, not a datasheet
#: number: the cited gain, bandwidth and slew rate set the load resistance, the compensation
#: capacitor and the slew current around it.
_GM = 1e-3


class OpAmpDesignError(ValueError):
    """A recognised op amp cannot be rendered faithfully, or a saved design was altered."""


# name, unit, default, essential, sources. A source is (probe, field, transform); rows are
# read from channel 1 of the probe-bound rows only, so every input traces to a row that an
# independent test also judges. Only a typical value is a cited input: a bound alone says
# nothing about the typical device (except a minimum and a maximum together).
_PARAMETERS: tuple[tuple[str, str, float, bool, tuple[tuple[str, str], ...]], ...] = (
    ("VOS", "V", 1e-3, True, (("opamp_offset", "typ_value"),)),
    ("IB", "A", 50e-9, True, (("opamp_bias", "typ_value"),)),
    ("IOS", "A", 5e-9, False, (("opamp_offset_current", "typ_value"),)),
    ("AOL", "V/V", 1e5, True, (("opamp_gain", "typ_value"),)),
    ("GBW", "Hz", 1e6, True, (("opamp_gbw", "typ_value"),)),
    ("SR", "V/s", 5e5, True, (("opamp_slew_rise", "typ_value"), ("opamp_slew_fall", "typ_value"))),
    ("VOH_HEADROOM", "V", 1.5, True, (("opamp_swing_high", "typ_value"),)),
    ("VOL", "V", 0.02, True, (("opamp_swing_low", "typ_value"),)),
    ("IQ_CH", "A", 5e-4, True, (("opamp_quiescent", "typ_value"),)),
    ("ISC", "A", 20e-3, False, ()),
    ("ROUT", "ohm", 50.0, False, ()),
)
_NAMES = tuple(entry[0] for entry in _PARAMETERS)
ESSENTIAL_INPUTS = tuple(entry[0] for entry in _PARAMETERS if entry[3])

#: Essential behaviours: each needs at least one bound row for every probe listed, so a
#: supported claim is never made for a behaviour no independent test covers.
BEHAVIOURS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input offset voltage", ("opamp_offset",)),
    ("input bias current", ("opamp_bias",)),
    ("open-loop gain", ("opamp_gain",)),
    ("gain bandwidth", ("opamp_gbw",)),
    ("slew rate", ("opamp_slew_rise", "opamp_slew_fall")),
    ("output swing", ("opamp_swing_high", "opamp_swing_low")),
    ("supply current", ("opamp_quiescent",)),
)


def _spice(value: float) -> str:
    return format(value, ".12g")


def _channel_one(char: Characteristic) -> bool:
    return char.probe_params.get("op_channel", 1.0) == 1.0


def _sourced(
    spec: SpecSet, probe: str, field: str, unit: str, unverified: Collection[str]
) -> tuple[Characteristic, float] | None:
    """The first channel-1 row bound to ``probe`` that cites a usable number in ``field``."""
    for char in spec.characteristics:
        if char.probe != probe or char.char_id in unverified or not _channel_one(char):
            continue
        if char.req_class == "ABSOLUTE_MAXIMUM" or char.unit != unit:
            continue
        if char.source_page is None or not char.excerpt.strip():
            continue
        raw = getattr(char, field)
        if raw is None or not math.isfinite(raw):
            continue
        return char, float(raw)
    return None


def _parameters(spec: SpecSet, unverified: Collection[str]) -> tuple[ParameterOrigin, ...]:
    chosen: list[ParameterOrigin] = []
    for name, unit, default, _essential, sources in _PARAMETERS:
        found = None
        for probe, field in sources:
            found = _sourced(spec, probe, field, unit, unverified)
            if found is not None:
                break
        if found is None:
            chosen.append(ParameterOrigin(name, default, unit, "template_default"))
            continue
        char, value = found
        chosen.append(
            ParameterOrigin(
                name=name,
                value=value,
                unit=unit,
                origin="cited_row",
                row_id=char.char_id,
                page=char.source_page,
                excerpt=char.excerpt,
                transform="none",
            )
        )
    _check_values({item.name: item.value for item in chosen})
    return tuple(chosen)


def _check_values(values: dict[str, float]) -> None:
    """Relations every renderable op amp must satisfy."""
    for name in ("AOL", "GBW", "SR", "IQ_CH", "ISC", "ROUT"):
        if values[name] <= 0:
            raise OpAmpDesignError(f"op_amp_invalid_parameter: {name}")
    for name in ("VOS", "IB", "IOS", "VOH_HEADROOM", "VOL"):
        if values[name] < 0:
            raise OpAmpDesignError(f"op_amp_invalid_parameter: {name}")
    if values["IOS"] > 2 * values["IB"]:
        raise OpAmpDesignError("op_amp_invalid_bias: the offset current exceeds twice the bias")


def _pin_roles(ports: tuple[str, ...]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for port in ports:
        name = port.upper()
        if name == "VCC":
            roles[port] = "supply"
        elif name == "VEE":
            roles[port] = "ground"
        elif name.startswith("OUT"):
            roles[port] = "output"
        else:
            roles[port] = "input"
    return roles


@dataclass(frozen=True)
class OpAmpDesign:
    """What to build, as data: the eight pins and every parameter with its origin."""

    renderer_version: str
    spec_digest: str
    part: str
    subckt: str
    ports: tuple[str, ...]
    parameters: tuple[ParameterOrigin, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.subckt, str) or not _SUBCKT_NAME.fullmatch(self.subckt):
            raise OpAmpDesignError("op_amp_invalid_subckt")
        if not isinstance(self.part, str) or not self.part.strip():
            raise OpAmpDesignError("op_amp_design_part")
        if not isinstance(self.spec_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.spec_digest
        ):
            raise OpAmpDesignError("op_amp_design_spec_digest")
        if {port.upper() for port in self.ports} != set(DUAL_PORTS) or len(self.ports) != len(
            DUAL_PORTS
        ):
            raise OpAmpDesignError("op_amp_design_pins: expected the eight dual-amplifier pins")
        if tuple(item.name for item in self.parameters) != _NAMES:
            raise OpAmpDesignError("op_amp_design_parameter_set")
        for item in self.parameters:
            if isinstance(item.value, bool) or not isinstance(item.value, int | float):
                raise OpAmpDesignError(f"op_amp_design_value_type: {item.name}")
            if not math.isfinite(item.value):
                raise OpAmpDesignError(f"op_amp_design_nonfinite: {item.name}")
            if item.origin not in _ORIGINS:
                raise OpAmpDesignError(f"op_amp_design_origin: {item.name}: {item.origin!r}")
            sourced = item.row_id is not None and item.page is not None and bool(item.excerpt)
            if (item.origin in _CITED_ORIGINS) != sourced:
                raise OpAmpDesignError(f"op_amp_design_provenance: {item.name}")
        _check_values({item.name: item.value for item in self.parameters})

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": DESIGN_SCHEMA_VERSION,
            "record_kind": "op_amp_design",
            "family": "dual_op_amp",
            "renderer_version": self.renderer_version,
            "spec_digest": self.spec_digest,
            "part": self.part,
            "subckt": self.subckt,
            "ports": list(self.ports),
            "pin_roles": _pin_roles(self.ports),
            "external_connections": [],
            "parameters": [item.payload() for item in self.parameters],
        }

    def to_json(self) -> str:
        """Canonical text: sorted keys, fixed indent, one trailing newline."""
        return json.dumps(self.payload(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def record(self, delivered: bytes | None) -> dict[str, Any]:
        return design_record_payload(self, delivered)

    @classmethod
    def from_payload(cls, data: Any) -> OpAmpDesign:
        """Read a saved design back; anything altered, incomplete or inconsistent is refused."""
        if (
            not isinstance(data, dict)
            or data.get("record_kind") != "op_amp_design"
            or data.get("schema_version") != DESIGN_SCHEMA_VERSION
        ):
            raise OpAmpDesignError("op_amp_design_record_kind")
        try:
            design = cls(
                renderer_version=data["renderer_version"],
                spec_digest=data["spec_digest"],
                part=data["part"],
                subckt=data["subckt"],
                ports=tuple(data["ports"]),
                parameters=tuple(
                    ParameterOrigin(
                        name=item["name"],
                        value=item["value"],
                        unit=item["unit"],
                        origin=item["origin"],
                        row_id=item.get("row_id"),
                        page=item.get("page"),
                        excerpt=item.get("excerpt"),
                        transform=item.get("transform"),
                    )
                    for item in data["parameters"]
                ),
            )
        except (KeyError, TypeError) as exc:
            raise OpAmpDesignError(f"op_amp_design_record_incomplete: {exc}") from exc
        if design.payload() != data:
            raise OpAmpDesignError("op_amp_design_record_altered")
        return design


def design_from_spec(spec: SpecSet, *, unverified: Collection[str] = ()) -> OpAmpDesign | None:
    """Return the design for a matching dual op amp, else ``None``.

    Recognition is narrow on purpose: the eight named dual-amplifier pins and at least one
    row bound to an op-amp probe, so a different part is never given this function and a
    claim is never made for a pinout the independent tests cannot drive.
    """
    from boardmodeler.authoring.pin_roles import physical_terminals

    if not spec.pin_map:
        return None
    try:
        ports = physical_terminals(spec.pin_map)
    except KeyError, ValueError:
        return None
    if len(ports) != len(DUAL_PORTS) or {port.upper() for port in ports} != set(DUAL_PORTS):
        return None
    if not any(
        (char.probe or "").startswith("opamp_")
        for char in spec.characteristics
        if char.char_id not in unverified
    ):
        return None
    if not _SUBCKT_NAME.fullmatch(spec.subckt):
        raise OpAmpDesignError("op_amp_invalid_subckt")
    return OpAmpDesign(
        renderer_version=OP_AMP_RENDERER_VERSION,
        spec_digest=spec.digest(),
        part=spec.part,
        subckt=spec.subckt,
        ports=ports,
        parameters=_parameters(spec, unverified),
    )


def _channel_pins(design: OpAmpDesign) -> list[tuple[int, str, str, str]]:
    """(channel, output, inverting, non-inverting) port names, in the order written."""
    by_upper = {port.upper(): port for port in design.ports}
    return [(n, by_upper[f"OUT{n}"], by_upper[f"IN{n}M"], by_upper[f"IN{n}P"]) for n in (1, 2)]


def render_library(design: OpAmpDesign) -> str:
    """The library text for ``design``: a pure function of the design and the renderer."""
    if design.renderer_version != OP_AMP_RENDERER_VERSION:
        raise OpAmpDesignError(
            f"op_amp_renderer_version: design {design.renderer_version!r}, "
            f"renderer {OP_AMP_RENDERER_VERSION!r}"
        )
    v = {item.name: item.value for item in design.parameters}
    roles = _pin_roles(design.ports)
    supply = next(port for port, kind in roles.items() if kind == "supply")
    ground = next(port for port, kind in roles.items() if kind == "ground")
    channels = _channel_pins(design)
    inverting = {inm for _n, _out, inm, _inp in channels}
    pins: list[ShellPin] = []
    for number, port in enumerate(design.ports, start=1):
        kind = roles[port]
        if kind == "supply":
            pins.append(ShellPin(port, "supply", str(number), iq=v["IQ_CH"] * len(channels)))
        elif kind == "ground":
            pins.append(ShellPin(port, "ground", str(number)))
        elif kind == "output":
            pins.append(
                ShellPin(
                    port,
                    "output",
                    str(number),
                    r_out=v["ROUT"],
                    i_source=v["ISC"],
                    i_sink=v["ISC"],
                )
            )
        else:
            offset = v["IOS"] / 2 if port in inverting else -v["IOS"] / 2
            pins.append(ShellPin(port, "input", str(number), clamp="gnd", ibias=v["IB"] + offset))
    ro = v["AOL"] / _GM
    cc = _GM / (2 * math.pi * v["GBW"])
    islew = v["SR"] * cc
    core: list[str] = []
    drives: dict[str, str] = {}
    for n, out, inm, inp in channels:
        core += [
            f"Bvos{n} d{n} {ground} V = V({inp},{ground}) - V({inm},{ground}) - {_spice(v['VOS'])}",
            f"Bgm{n} {ground} n{n} I = {_spice(islew)}*tanh({_spice(_GM)}*V(d{n},{ground})/{_spice(islew)})",
            f"R{n} n{n} {ground} {_spice(ro)}",
            f"C{n} n{n} {ground} {_spice(cc)}",
            f"Dclp{n}a n{n} {supply} Dpin",
            f"Dclp{n}b {ground} n{n} Dpin",
        ]
        drives[out] = (
            f"limit(V(n{n},{ground}),{_spice(v['VOL'])},"
            f"max(V({supply},{ground})-{_spice(v['VOH_HEADROOM'])},{_spice(v['VOL'])}))"
        )
    return render_shell(
        design.subckt,
        pins,
        core="\n".join(core),
        drives=drives,
        title=f"{design.part} dual op amp: pin shell + op-amp core",
    )


def design_record_payload(design: OpAmpDesign, delivered: bytes | None) -> dict[str, Any]:
    """The record that ties a design to the library bytes actually delivered."""
    rendered = render_library(design).encode("utf-8")
    if delivered is None:
        association, note = "not_delivered", "No library was delivered for this design."
    elif delivered == rendered:
        association = "exact"
        note = "The delivered library is byte-for-byte the rendering of this design."
    else:
        association = "invalid_after_change"
        note = (
            "The delivered library differs from the rendering of this design. The design "
            "describes the starting candidate only; final parameters are not reconstructed "
            "from SPICE text."
        )
    return {
        "schema_version": DESIGN_SCHEMA_VERSION,
        "record_kind": "model_design_record",
        "design_sha256": design.sha256,
        "design": design.payload(),
        "rendered_library_sha256": hashlib.sha256(rendered).hexdigest(),
        "delivered_library_sha256": (
            None if delivered is None else hashlib.sha256(delivered).hexdigest()
        ),
        "association": association,
        "association_note": note,
        "verdict": "UNJUDGED",
        "verdict_note": "Design provenance is not electrical proof; the LTspice harness decides.",
    }


@dataclass(frozen=True)
class OpAmpSeed:
    """A rendered candidate and the design it came from (the interface the pipeline seeds from)."""

    design: OpAmpDesign
    library_text: str

    def payload(self) -> dict[str, Any]:
        """Evidence for a card or a neighbouring ``template-parameters.json``."""
        return {
            "schema_version": 1,
            "template_kind": "dual_op_amp",
            "renderer_version": self.design.renderer_version,
            "spec_digest": self.design.spec_digest,
            "part": self.design.part,
            "subckt": self.design.subckt,
            "ports": list(self.design.ports),
            "parameters": [item.payload() for item in self.design.parameters],
            "verdict": "UNJUDGED",
            "verdict_note": "Run the LTspice harness; parameter provenance is not electrical proof.",
        }

    def write(self, path: Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.library_text, encoding="utf-8", newline="\n")
        return target


def seed_from_spec(spec: SpecSet, *, unverified: Collection[str] = ()) -> OpAmpSeed | None:
    """A candidate for a matching dual op amp, else ``None``."""
    design = design_from_spec(spec, unverified=unverified)
    if design is None:
        return None
    return OpAmpSeed(design=design, library_text=render_library(design))
