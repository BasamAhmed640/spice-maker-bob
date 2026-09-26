"""Deterministic full-deck checks for an exposed-COMP peak-current buck.

M2 uses cited frozen rows and named bench parts to build gain, ILIM-corner,
ripple, PH-edge, startup and resistive load-step tests. The only model edit is
an *instance* ``ILIM=<cited bound>`` override for the two template parameter
corners; the delivered library is unchanged. LTspice 26 was separately observed
honouring an instance override of a subcircuit-local ``.param``. This is a
template-parameter corner, not proof of a silicon process corner.

``evaluate_waveform`` returns measurements or UNKNOWN with a reason. It never
awards a datasheet PASS; the verification engine must apply cited limits and
record simulator artifact hashes before publishing a verdict.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import numpy as np

from boardmodeler.authoring.pin_roles import physical_terminals
from boardmodeler.authoring.spec import Characteristic, SpecSet, normalize_unit
from boardmodeler.models.buck_switching import match_pins
from boardmodeler.models.library import subckt_ports
from boardmodeler.simulation.raw import RawFile

__all__ = [
    "BuckBench",
    "BuckBenchParts",
    "BuckObservation",
    "CitedRow",
    "GainPoint",
    "build_buck_system_benches",
    "evaluate_waveform",
    "render_deck",
]

Kind = Literal["gain", "limit_min", "limit_max", "ripple", "edge", "load_step", "startup"]
_ROLE_NODES = {
    "BOOT": "boot",
    "VIN": "vin",
    "EN": "en",
    "SS": "ss",
    "VSENSE": "vsense",
    "COMP": "comp",
    "GND": "0",
    "PH": "ph",
}
_CATCH_MODEL = ".model BM_CATCH D(Is=1e-8 N=1.1 Rs=0.04 Cjo=300p Bv=40 Ibv=1m)"


@dataclass(frozen=True)
class CitedRow:
    char_id: str
    statement: str
    unit: str
    source_page: int  # zero-indexed PDF page, as in Characteristic
    excerpt: str
    min_value: float | None
    typ_value: float | None
    max_value: float | None

    @classmethod
    def from_characteristic(cls, value: Characteristic) -> CitedRow:
        if value.source_page is None or not value.excerpt.strip():
            raise ValueError(f"{value.char_id}: no cited page and excerpt")
        return cls(
            value.char_id,
            value.statement,
            value.unit,
            value.source_page,
            value.excerpt,
            value.min_value,
            value.typ_value,
            value.max_value,
        )


@dataclass(frozen=True)
class BuckBenchParts:
    """Explicit laboratory passives, not claims about the device or TI silicon."""

    source: str = "TI-derived TPS54332 M1 passive deck; values are bench choices"
    vin_v: float = 12.0
    input_cap_f: float = 10e-6
    ss_cap_f: float = 15e-9
    edge_ss_cap_f: float = 1e-9
    boot_cap_f: float = 100e-9
    inductance_h: float = 2.5e-6
    inductor_r_ohm: float = 0.01
    output_cap_f: float = 47e-6
    output_cap_esr_ohm: float = 0.003
    fb_high_ohm: float = 10.2e3
    fb_low_ohm: float = 4.75e3
    comp_r_ohm: float = 75e3
    comp_c_f: float = 180e-12
    comp_hf_f: float = 10e-12
    pre_step_load_a: float = 1.0
    post_step_load_a: float = 3.0
    short_ohm: float = 0.01

    def __post_init__(self) -> None:
        numbers = {
            name: value for name, value in vars(self).items() if name not in {"source", "vin_v"}
        }
        if (
            not self.source.strip()
            or "\n" in self.source
            or "\r" in self.source
            or not math.isfinite(self.vin_v)
            or self.vin_v <= 0
        ):
            raise ValueError("bench parts need a named source and positive VIN")
        if any(not math.isfinite(value) or value <= 0 for value in numbers.values()):
            raise ValueError("bench parts must be finite and positive")
        if self.post_step_load_a <= self.pre_step_load_a:
            raise ValueError("load step must increase current")

    def output_target_v(self, vref_v: float) -> float:
        value = vref_v * (1 + self.fb_high_ohm / self.fb_low_ohm)
        if not 0 < value < self.vin_v:
            raise ValueError("feedback divider requires 0 < target output < VIN")
        return value


@dataclass(frozen=True)
class GainPoint:
    comp_v: float
    early_window_s: tuple[float, float]
    late_window_s: tuple[float, float]
    measure_name: str


@dataclass(frozen=True)
class BuckBench:
    name: str
    kind: Kind
    part: str
    subckt: str
    ports: tuple[str, ...]
    nodes: tuple[str, ...]
    source_rows: tuple[CitedRow, ...]
    passive_source: str
    vin_v: float
    components: tuple[str, ...]
    measures: tuple[str, ...]
    saved_signals: tuple[str, ...]
    stop_s: float
    max_step_s: float
    window_s: tuple[float, float]
    metadata: dict[str, Any] = field(default_factory=dict)
    gain_points: tuple[GainPoint, ...] = ()
    ilim_instance_a: float | None = None


@dataclass(frozen=True)
class BuckObservation:
    kind: Kind
    status: Literal["MEASURED", "UNKNOWN"]
    metrics: dict[str, Any]
    reason: str | None = None


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("nonfinite bench value")
    return format(value, ".12g")


def _spice_comment(value: str) -> str:
    """Keep deck comments single-line ASCII; full citations stay in metadata."""
    return " ".join(value.split()).encode("ascii", "backslashreplace").decode("ascii")[:180]


def _verified_requirements(path: Path, spec: SpecSet) -> dict[str, dict[str, Any]]:
    """Read the extraction record, where citation_verified is retained."""
    source = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(source, dict) or not isinstance(source.get("requirements"), list):
        raise ValueError("requirements source has no requirements list")
    if source.get("document", {}).get("doc_id") != spec.doc_id:
        raise ValueError("requirements document does not match frozen spec")
    result: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for item in source["requirements"]:
        if not isinstance(item, dict) or not isinstance(item.get("req_id"), str):
            raise ValueError("requirements source has a malformed row")
        identifier = item["req_id"]
        if identifier in seen:
            raise ValueError(f"duplicate requirement {identifier}")
        seen.add(identifier)
        if item.get("citation_verified") is True:
            result[identifier] = item
    return result


def _check_verified_row(row: CitedRow, requirement: dict[str, Any], doc_id: str) -> None:
    evidence = requirement.get("evidence") or []
    if not evidence or not isinstance(evidence[0], dict):
        raise ValueError(f"{row.char_id}: verified citation has no evidence")
    cited = evidence[0]
    if (
        cited.get("doc_id") != doc_id
        or cited.get("excerpt") != row.excerpt
        or cited.get("page", {}).get("pdf_page") != row.source_page
    ):
        raise ValueError(f"{row.char_id}: frozen citation differs from verified source")
    limits = requirement.get("limits")
    if not isinstance(limits, dict):
        raise ValueError(f"{row.char_id}: verified source has no numeric limits")
    unit, scale = normalize_unit(str(limits.get("unit", "")))
    if unit != row.unit:
        raise ValueError(f"{row.char_id}: source and spec units differ")
    for field_name, key in (("min_value", "min"), ("typ_value", "typ"), ("max_value", "max")):
        frozen = getattr(row, field_name)
        source_value = limits.get(key)
        if (frozen is None) != (source_value is None):
            raise ValueError(f"{row.char_id}: source and spec {key} differ")
        if frozen is not None and not math.isclose(
            frozen, float(source_value) * scale, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise ValueError(f"{row.char_id}: source and spec {key} differ")


def _row(
    spec: SpecSet,
    verified: dict[str, dict[str, Any]],
    pattern: str,
    unit: str,
    fields: tuple[str, ...],
) -> CitedRow:
    candidates = [
        value
        for value in spec.characteristics
        if value.unit == unit
        and value.char_id in verified
        and value.req_class.upper() != "ABSOLUTE_MAXIMUM"
        and re.search(pattern, value.statement, re.I)
        and value.source_page is not None
        and value.excerpt.strip()
        and all(getattr(value, name) is not None for name in fields)
    ]
    if not candidates:
        raise ValueError(f"no citation-checked {unit} row for {pattern}")
    candidates.sort(
        key=lambda value: (
            -sum(x is not None for x in (value.min_value, value.typ_value, value.max_value)),
            value.char_id,
        )
    )
    row = CitedRow.from_characteristic(candidates[0])
    _check_verified_row(row, verified[row.char_id], spec.doc_id)
    return row


def _sources(spec: SpecSet, verified: dict[str, dict[str, Any]]) -> dict[str, CitedRow]:
    return {
        "vref": _row(
            spec,
            verified,
            r"\b(?:voltage|feedback) reference\b|\breference voltage\b",
            "V",
            ("typ_value",),
        ),
        "fsw": _row(spec, verified, r"\bswitching frequency\b", "Hz", ("typ_value",)),
        "ilim": _row(
            spec, verified, r"\bcurrent limit threshold\b", "A", ("min_value", "max_value")
        ),
        "gmcs": _row(
            spec,
            verified,
            r"switch current to COMP transconductance|current[- ]sense gain",
            "A/V",
            ("typ_value",),
        ),
        "veco": _row(
            spec,
            verified,
            r"\bCOMP\b[^.]*(?:Eco[- ]?mode|pulse[- ]skip)|(?:Eco[- ]?mode|pulse[- ]skip)[^.]*\bCOMP\b",
            "V",
            ("typ_value",),
        ),
        "iss": _row(
            spec,
            verified,
            r"(?:slow|soft)[- ]?start[^.]*charge current|\bSS\b[^.]*charge current",
            "A",
            ("typ_value",),
        ),
    }


def _common_components(parts: BuckBenchParts, vout_v: float, ss_cap_f: float) -> tuple[str, ...]:
    base_r = vout_v / parts.pre_step_load_a
    return (
        f"Vin vin 0 PULSE(0 {_number(parts.vin_v)} 0 1u 1u 30m 60m)",
        f"Cin vin 0 {_number(parts.input_cap_f)}",
        f"Ven en 0 {_number(min(3.3, parts.vin_v))}",
        f"Css ss 0 {_number(ss_cap_f)}",
        f"Cboot boot ph {_number(parts.boot_cap_f)}",
        "Dcatch 0 ph BM_CATCH",
        f"Lout ph out {_number(parts.inductance_h)} Rser={_number(parts.inductor_r_ohm)}",
        f"Cout1 out 0 {_number(parts.output_cap_f)} Rser={_number(parts.output_cap_esr_ohm)}",
        f"Cout2 out 0 {_number(parts.output_cap_f)} Rser={_number(parts.output_cap_esr_ohm)}",
        f"Rload out 0 {_number(base_r)}",
        f"Rfbhi out vsense {_number(parts.fb_high_ohm)}",
        f"Rfblo vsense 0 {_number(parts.fb_low_ohm)}",
        f"Rcomp comp compmid {_number(parts.comp_r_ohm)}",
        f"Ccomp compmid 0 {_number(parts.comp_c_f)}",
        f"Chf comp 0 {_number(parts.comp_hf_f)}",
    )


def _switched_resistor(name: str, resistance_ohm: float, at_s: float) -> tuple[str, ...]:
    return (
        f"R{name} out {name.lower()}_node {_number(resistance_ohm)}",
        f"S{name} {name.lower()}_node 0 {name.lower()}_ctl 0 BM_{name.upper()}_SW",
        f"V{name} {name.lower()}_ctl 0 PULSE(0 1 {_number(at_s)} 1n 1n 20m 40m)",
        f".model BM_{name.upper()}_SW SW(Ron=1m Roff=1G Vt=0.5 Vh=0)",
    )


def _gain_waveform(levels: tuple[float, ...], start_s: float, hold_s: float) -> str:
    points = [(0.0, levels[0])]
    for index, level in enumerate(levels):
        at = start_s + index * hold_s
        points.append((at, levels[index - 1] if index else level))
        if index:
            points.append((at + 1e-6, level))
        points.append((at + hold_s - 1e-6, level))
    pairs = " ".join(f"{_number(at)} {_number(value)}" for at, value in points)
    return f"Vforce comp 0 PWL({pairs})"


def build_buck_system_benches(
    spec: SpecSet,
    parts: BuckBenchParts | None = None,
    *,
    requirements_path: Path,
) -> tuple[BuckBench, ...]:
    """Build seven checks using verified raw citation records and frozen values."""
    parts = parts or BuckBenchParts()
    rows = _sources(spec, _verified_requirements(requirements_path, spec))
    values = {
        "vref_schedule": (
            rows["vref"].max_value if rows["vref"].max_value is not None else rows["vref"].typ_value
        ),
        "vref": rows["vref"].typ_value,
        "fsw": rows["fsw"].typ_value,
        "ilim_min": rows["ilim"].min_value,
        "ilim_max": rows["ilim"].max_value,
        "gmcs": rows["gmcs"].typ_value,
        "veco": rows["veco"].typ_value,
        "iss": rows["iss"].typ_value,
    }
    if any(value is None or not math.isfinite(value) or value <= 0 for value in values.values()):
        raise ValueError("cited buck values must be finite and positive")
    if values["ilim_min"] >= values["ilim_max"]:
        raise ValueError("cited current-limit bounds are not ordered")
    ports = physical_terminals(spec.pin_map)
    pins = match_pins(ports)
    if not pins.ok:
        raise ValueError(pins.reason)
    by_port = {port: _ROLE_NODES[role] for role, port in pins.roles.items()}
    by_port.update({port: "0" for port in pins.ground_ties})
    nodes = tuple(by_port[port] for port in ports)
    vout = parts.output_target_v(values["vref"])
    # ISS has only a cited typical value; this is a conservative schedule
    # with the latest cited VREF, not a worst-case silicon timing guarantee.
    ss_s = parts.ss_cap_f * values["vref_schedule"] / values["iss"]
    if not 0 < ss_s < 0.05:
        raise ValueError("cited soft-start and bench capacitor make an unsupported >50 ms test")
    base = _common_components(parts, vout, parts.ss_cap_f)
    shared = (rows["vref"], rows["iss"], rows["fsw"])
    common = dict(
        part=spec.part,
        subckt=spec.subckt,
        ports=ports,
        nodes=nodes,
        passive_source=parts.source,
        vin_v=parts.vin_v,
    )
    results: list[BuckBench] = []

    # The production gain deck starts strictly ABOVE the cited Eco threshold.
    gain_start = ss_s + 1e-3
    gain_hold = 500e-6
    max_count = max(3, math.ceil(1.2 * values["ilim_max"] / (0.05 * values["gmcs"])))
    if max_count > 20:
        raise ValueError("gain fixture needs more than 20 COMP levels to cover cited ILIM")
    levels = tuple(round(values["veco"] + 0.05 * (index + 1), 8) for index in range(max_count))
    if not all(level > values["veco"] for level in levels):
        raise ValueError("gain fixture has a COMP point at or below cited VECO")
    points = tuple(
        GainPoint(
            level,
            (gain_start + i * gain_hold + 150e-6, gain_start + i * gain_hold + 300e-6),
            (gain_start + i * gain_hold + 350e-6, gain_start + (i + 1) * gain_hold - 5e-6),
            f"gain_p{i + 1:02d}",
        )
        for i, level in enumerate(levels)
    )
    gain_end = gain_start + max_count * gain_hold + 50e-6
    gain_shunt = vout / (1.5 * values["ilim_max"])
    gain_components = (
        *base,
        f"Rgain out 0 {_number(gain_shunt)}",
        f"Vfb vsense 0 {_number(values['vref'])}",
        _gain_waveform(levels, gain_start, gain_hold),
    )
    results.append(
        BuckBench(
            "gain",
            "gain",
            source_rows=(*shared, rows["gmcs"], rows["veco"], rows["ilim"]),
            components=gain_components,
            measures=tuple(
                f".meas tran {point.measure_name} MAX I(Lout) FROM={_number(point.late_window_s[0])} TO={_number(point.late_window_s[1])}"
                for point in points
            ),
            saved_signals=("V(ph)", "V(out)", "I(Lout)", "V(comp)", "V(vsense)"),
            stop_s=gain_end,
            max_step_s=50e-9,
            window_s=(gain_start, gain_end),
            gain_points=points,
            metadata={
                "veco_v": values["veco"],
                "gmcs_typ_a_per_v": values["gmcs"],
                "gain_shunt_ohm": gain_shunt,
                "nominal_fsw_hz": values["fsw"],
                "ss_timing_vref_v": values["vref_schedule"],
                "iss_typ_a": values["iss"],
            },
            **common,
        )
    )

    switch_at = ss_s + 1e-3
    limit_start = switch_at + 0.6e-3
    limit_end = limit_start + 1e-3
    for name, bound in (("limit_min", values["ilim_min"]), ("limit_max", values["ilim_max"])):
        target_load_a = 1.5 * bound
        conductance = target_load_a / vout - parts.pre_step_load_a / vout
        if conductance <= 0:
            raise ValueError("current-limit overload cannot exceed base resistive load")
        fault_r = 1 / conductance
        results.append(
            BuckBench(
                name,
                name,
                source_rows=(*shared, rows["ilim"]),
                components=base + _switched_resistor("fault", fault_r, switch_at),
                measures=(
                    f".meas tran ilim_peak MAX I(Lout) FROM={_number(limit_start)} TO={_number(limit_end)}",
                    f".meas tran ilim_avg AVG I(Lout) FROM={_number(limit_start)} TO={_number(limit_end)}",
                ),
                saved_signals=("V(ph)", "V(out)", "V(vsense)", "I(Lout)"),
                stop_s=limit_end + 0.1e-3,
                max_step_s=50e-9,
                window_s=(limit_start, limit_end),
                ilim_instance_a=bound,
                metadata={
                    "corner": "minimum" if name.endswith("min") else "maximum",
                    "cited_bound_a": bound,
                    "switch_at_s": switch_at,
                    "fault_resistor_ohm": fault_r,
                    "nominal_fsw_hz": values["fsw"],
                    "corner_kind": "template_instance_parameter_not_silicon_process",
                },
                **common,
            )
        )

    steady_start = ss_s + 1.3e-3
    steady_end = steady_start + 1e-3
    results.append(
        BuckBench(
            "ripple",
            "ripple",
            source_rows=shared,
            components=base,
            measures=(
                f".meas tran ripple_hi MAX V(out) FROM={_number(steady_start)} TO={_number(steady_end)}",
                f".meas tran ripple_lo MIN V(out) FROM={_number(steady_start)} TO={_number(steady_end)}",
            ),
            saved_signals=("V(out)", "V(ph)"),
            stop_s=steady_end + 0.1e-3,
            max_step_s=50e-9,
            window_s=(steady_start, steady_end),
            metadata={"nominal_fsw_hz": values["fsw"], "vout_target_v": vout},
            **common,
        )
    )

    edge_ss_s = parts.edge_ss_cap_f * values["vref_schedule"] / values["iss"]
    edge_start = edge_ss_s + 0.25e-3
    edge_end = edge_start + 0.1e-3
    edge_stop = edge_end + 0.05e-3
    edge_step = 2e-9
    if edge_stop / edge_step > 1_000_000:
        raise ValueError("edge bench needs shorter SS capacitor for resolvable post-startup edges")
    low, high = 0.1 * parts.vin_v, 0.9 * parts.vin_v
    edge_base = _common_components(parts, vout, parts.edge_ss_cap_f)
    results.append(
        BuckBench(
            "edge",
            "edge",
            source_rows=shared,
            components=edge_base,
            measures=(
                f".meas tran ph_rise TRIG V(ph) VAL={_number(low)} RISE=1 TD={_number(edge_start)} TARG V(ph) VAL={_number(high)} RISE=1 TD={_number(edge_start)}",
                f".meas tran ph_fall TRIG V(ph) VAL={_number(high)} FALL=1 TD={_number(edge_start)} TARG V(ph) VAL={_number(low)} FALL=1 TD={_number(edge_start)}",
            ),
            saved_signals=("V(ph)", "V(out)"),
            stop_s=edge_stop,
            max_step_s=edge_step,
            window_s=(edge_start, edge_end),
            metadata={
                "low_v": low,
                "high_v": high,
                "nominal_fsw_hz": values["fsw"],
                "edge_ss_cap_f": parts.edge_ss_cap_f,
            },
            **common,
        )
    )

    load_at = ss_s + 1e-3
    extra_a = parts.post_step_load_a - parts.pre_step_load_a
    added_r = vout / extra_a
    load_final_start = load_at + 0.8e-3
    load_final_end = load_final_start + 0.4e-3
    results.append(
        BuckBench(
            "load_step",
            "load_step",
            source_rows=shared,
            components=base + _switched_resistor("step", added_r, load_at),
            measures=(
                f".meas tran load_pre AVG V(out) FROM={_number(load_at - 0.3e-3)} TO={_number(load_at - 0.05e-3)}",
                f".meas tran load_min MIN V(out) FROM={_number(load_at)} TO={_number(load_at + 0.5e-3)}",
                f".meas tran load_final AVG V(out) FROM={_number(load_final_start)} TO={_number(load_final_end)}",
            ),
            saved_signals=("V(out)", "V(ph)", "I(Lout)"),
            stop_s=load_final_end + 0.1e-3,
            max_step_s=50e-9,
            window_s=(load_at - 0.3e-3, load_final_end),
            metadata={
                "switch_at_s": load_at,
                "final_window_s": (load_final_start, load_final_end),
                "pre_load_a": parts.pre_step_load_a,
                "post_load_a": parts.post_step_load_a,
                "added_resistor_ohm": added_r,
                "nominal_fsw_hz": values["fsw"],
            },
            **common,
        )
    )

    startup_stop = ss_s + 2e-3
    results.append(
        BuckBench(
            "startup",
            "startup",
            source_rows=(rows["vref"], rows["iss"]),
            components=base,
            measures=(
                f".meas tran start_t10 WHEN V(out)={_number(0.1 * vout)} RISE=1",
                f".meas tran start_t90 WHEN V(out)={_number(0.9 * vout)} RISE=1",
                f".meas tran start_max MAX V(out) FROM=0 TO={_number(startup_stop)}",
            ),
            saved_signals=("V(out)", "V(ss)", "V(vin)"),
            stop_s=startup_stop,
            max_step_s=50e-9,
            window_s=(0.0, startup_stop),
            metadata={
                "vout_target_v": vout,
                "nominal_soft_start_s": ss_s,
                "nominal_fsw_hz": values["fsw"],
            },
            **common,
        )
    )
    return tuple(results)


def render_deck(bench: BuckBench, model_library: Path) -> str:
    """Render a complete LTspice deck, validating exact physical port order."""
    model_path = Path(model_library).resolve(strict=True)
    if any(char in str(model_path) for char in ('"', "\r", "\n")):
        raise ValueError("model library path cannot be quoted in a SPICE include")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bench.subckt):
        raise ValueError("subcircuit name is not a SPICE identifier")
    model_text = model_path.read_text(encoding="utf-8")
    found = subckt_ports(model_text, bench.subckt)
    if tuple(name.upper() for name in found) != tuple(name.upper() for name in bench.ports):
        raise ValueError(f"physical_pin_contract: library {found}; cited {bench.ports}")
    if bench.ilim_instance_a is not None and not re.search(
        r"(?im)^\.param\s+[^\n]*\bILIM\s*=", model_text
    ):
        raise ValueError("current-limit corner needs a template with local ILIM .param")
    instance = f"XU1 {' '.join(bench.nodes)} {bench.subckt}"
    if bench.ilim_instance_a is not None:
        instance += f" ILIM={_number(bench.ilim_instance_a)}"
    citations = [
        f"* cited {row.char_id} PDF-page {row.source_page + 1}: " + _spice_comment(row.excerpt)
        for row in bench.source_rows
    ]
    lines = [
        f"* Deterministic M2 buck {bench.name}; no electrical verdict in deck",
        f"* passives: {_spice_comment(bench.passive_source)}",
        *citations,
        f'.include "{model_path.as_posix()}"',
        *bench.components,
        _CATCH_MODEL,
        instance,
        ".temp 25",
        f".tran 0 {_number(bench.stop_s)} 0 {_number(bench.max_step_s)}",
        ".save " + " ".join(bench.saved_signals),
        *bench.measures,
        ".end",
        "",
    ]
    return "\n".join(lines)


def _axis_and_signals(raw: RawFile, names: tuple[str, ...]) -> tuple[np.ndarray, ...]:
    axis = raw.time_column()
    if axis is None:
        raise ValueError("raw waveform has no time axis")
    t = np.asarray(axis, dtype=float)
    if len(t) < 3 or not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ValueError("raw waveform has an invalid time axis")
    signals = tuple(np.asarray(raw.column(name), dtype=float) for name in names)
    if any(len(signal) != len(t) or not np.all(np.isfinite(signal)) for signal in signals):
        raise ValueError("raw waveform has missing or nonfinite signals")
    return t, *signals


def _window(t: np.ndarray, start: float, end: float) -> slice:
    left = int(np.searchsorted(t, start))
    right = int(np.searchsorted(t, end, side="right"))
    if right - left < 3:
        raise ValueError(f"waveform window {start:g}..{end:g} s is not covered")
    return slice(left, right)


def _crossings(t: np.ndarray, signal: np.ndarray, level: float, rising: bool) -> np.ndarray:
    if rising:
        edges = np.flatnonzero((signal[:-1] < level) & (signal[1:] >= level))
    else:
        edges = np.flatnonzero((signal[:-1] > level) & (signal[1:] <= level))
    if not len(edges):
        return np.array([], dtype=float)
    left, right = signal[edges], signal[edges + 1]
    fraction = (level - left) / (right - left)
    return t[edges] + fraction * (t[edges + 1] - t[edges])


def _cycle_peaks(
    t: np.ndarray,
    ph: np.ndarray,
    current: np.ndarray,
    start: float,
    end: float,
    threshold: float,
) -> tuple[float | None, int, float | None]:
    region = _window(t, start, end)
    tt, vv, ii = t[region], ph[region], current[region]
    edges = _crossings(tt, vv, threshold, True)
    peaks = []
    for left, right in pairwise(edges):
        section = slice(int(np.searchsorted(tt, left)), int(np.searchsorted(tt, right)))
        if section.stop - section.start >= 2:
            peaks.append(float(np.max(ii[section])))
    if not peaks:
        return None, 0, None
    return float(np.median(peaks)), len(peaks), float(np.max(peaks))


def _frequency(
    t: np.ndarray, ph: np.ndarray, start: float, end: float, threshold: float
) -> tuple[float | None, int]:
    region = _window(t, start, end)
    edges = _crossings(t[region], ph[region], threshold, True)
    periods = np.diff(edges)
    periods = periods[periods > 0]
    return (float(1 / np.median(periods)), len(edges)) if len(periods) >= 2 else (None, len(edges))


def _linear_segment(
    points: list[dict[str, Any]], cited_gain_a_per_v: float
) -> dict[str, Any] | None:
    """Choose the longest stable contiguous region with consistent positive slopes."""
    best: tuple[int, int, float, float, float] | None = None
    for start in range(len(points)):
        for stop in range(start + 3, len(points) + 1):
            chosen = points[start:stop]
            if any(not point["stable"] or point["peak_a"] is None for point in chosen):
                continue
            x = np.array([point["comp_v"] for point in chosen], dtype=float)
            y = np.array([point["peak_a"] for point in chosen], dtype=float)
            slopes = np.diff(y) / np.diff(x)
            middle = float(np.median(slopes))
            if (
                middle < 0.2 * cited_gain_a_per_v
                or np.any(slopes <= 0)
                or np.any(abs(slopes - middle) > 0.2 * middle)
            ):
                continue
            slope, intercept = np.polyfit(x, y, 1)
            residual = float(np.max(abs(y - (slope * x + intercept))))
            if residual > max(0.1, 0.05 * float(np.ptp(y))):
                continue
            if best is None or stop - start > best[1] - best[0]:
                best = (start, stop, float(slope), float(intercept), residual)
    if best is None:
        return None
    start, stop, slope, intercept, residual = best
    return {
        "selected_comp_v": [point["comp_v"] for point in points[start:stop]],
        "fit_points": stop - start,
        "slope_a_per_v": slope,
        "intercept_a": intercept,
        "fit_max_abs_residual_a": residual,
        "selection_rule": "longest contiguous >=3 stable points; median slope >=20% cited gain, positive adjacent slopes within 20% of their median; residual <=max(0.10 A,5% span)",
    }


def _edge_measure(t: np.ndarray, ph: np.ndarray, bench: BuckBench, rising: bool) -> dict[str, Any]:
    region = _window(t, *bench.window_s)
    tt, vv = t[region], ph[region]
    low = float(bench.metadata["low_v"])
    high = float(bench.metadata["high_v"])
    first_level, last_level = (low, high) if rising else (high, low)
    starts = _crossings(tt, vv, first_level, rising)
    ends = _crossings(tt, vv, last_level, rising)
    max_width = 0.5 / float(bench.metadata["nominal_fsw_hz"])
    resolved: list[float] = []
    for first in starts:
        possible = ends[(ends >= first) & (ends <= first + max_width)]
        if not len(possible):
            continue
        last = float(possible[0])
        section = slice(int(np.searchsorted(tt, first)), int(np.searchsorted(tt, last)))
        interior = int(np.count_nonzero((vv[section] > low) & (vv[section] < high)))
        if interior >= 2:
            resolved.append(last - float(first))
    return {
        "transitions": len(starts),
        "resolved_transitions": len(resolved),
        "time_10_90_s" if rising else "time_90_10_s": float(np.median(resolved))
        if resolved
        else None,
        "reason": None if resolved else "fewer than two interior voltage samples per edge",
    }


def _binned_mean(
    t: np.ndarray, signal: np.ndarray, start: float, end: float, width: float
) -> tuple[np.ndarray, np.ndarray]:
    centers: list[float] = []
    values: list[float] = []
    for at in np.arange(start, end, width):
        left = int(np.searchsorted(t, at))
        right = int(np.searchsorted(t, min(at + width, end)))
        if right - left >= 2:
            centers.append(float(at + width / 2))
            values.append(float(np.mean(signal[left:right])))
    return np.asarray(centers), np.asarray(values)


def _evaluate(bench: BuckBench, raw: RawFile) -> BuckObservation:
    if bench.kind == "gain":
        t, ph, current = _axis_and_signals(raw, ("V(ph)", "I(Lout)"))
        threshold = 0.5 * bench.vin_v
        points: list[dict[str, Any]] = []
        for point in bench.gain_points:
            early, early_cycles, _ = _cycle_peaks(t, ph, current, *point.early_window_s, threshold)
            late, late_cycles, _ = _cycle_peaks(t, ph, current, *point.late_window_s, threshold)
            delta = abs(late - early) if early is not None and late is not None else None
            stable = (
                delta is not None
                and early_cycles >= 10
                and late_cycles >= 10
                and delta <= max(0.05, 0.05 * abs(late))
            )
            points.append(
                {
                    "comp_v": point.comp_v,
                    "early_peak_a": early,
                    "peak_a": late,
                    "early_cycles": early_cycles,
                    "cycles": late_cycles,
                    "early_late_delta_a": delta,
                    "stable": stable,
                }
            )
        fit = _linear_segment(points, float(bench.metadata["gmcs_typ_a_per_v"]))
        metrics = {"points": points, "cited_veco_v": bench.metadata["veco_v"], "fit": fit}
        if fit is None:
            return BuckObservation(
                bench.kind, "UNKNOWN", metrics, "fewer than three stable, linear above-VECO points"
            )
        return BuckObservation(bench.kind, "MEASURED", metrics)

    if bench.kind in {"limit_min", "limit_max"}:
        t, ph, current, vsense = _axis_and_signals(raw, ("V(ph)", "I(Lout)", "V(vsense)"))
        threshold = 0.5 * bench.vin_v
        peak, cycles, max_peak = _cycle_peaks(t, ph, current, *bench.window_s, threshold)
        frequency, edges = _frequency(t, ph, *bench.window_s, threshold)
        region = _window(t, *bench.window_s)
        metrics = {
            "corner": bench.metadata["corner"],
            "corner_kind": bench.metadata["corner_kind"],
            "instance_ilim_a": bench.ilim_instance_a,
            "median_cycle_peak_a": peak,
            "maximum_cycle_peak_a": max_peak,
            "average_inductor_a": float(np.mean(current[region])),
            "ph_frequency_hz": frequency,
            "vsense_min_v": float(np.min(vsense[region])),
            "vsense_max_v": float(np.max(vsense[region])),
            "cycles": cycles,
            "ph_edges": edges,
        }
        if peak is None or frequency is None or cycles < 10:
            return BuckObservation(
                bench.kind, "UNKNOWN", metrics, "insufficient post-soft-start switching cycles"
            )
        return BuckObservation(bench.kind, "MEASURED", metrics)

    if bench.kind == "ripple":
        t, out = _axis_and_signals(raw, ("V(out)",))
        region = _window(t, *bench.window_s)
        values = out[region]
        middle = len(values) // 2
        mean = float(np.mean(values))
        drift = abs(float(np.mean(values[:middle]) - np.mean(values[middle:])))
        metrics = {
            "vout_mean_v": mean,
            "ripple_pp_v": float(np.ptp(values)),
            "half_window_drift_v": drift,
        }
        if drift > max(0.01 * abs(mean), 0.5 * float(np.ptp(values))):
            return BuckObservation(
                bench.kind, "UNKNOWN", metrics, "output not settled across ripple window"
            )
        return BuckObservation(bench.kind, "MEASURED", metrics)

    if bench.kind == "edge":
        t, ph = _axis_and_signals(raw, ("V(ph)",))
        rise = _edge_measure(t, ph, bench, True)
        fall = _edge_measure(t, ph, bench, False)
        metrics = {"rise": rise, "fall": fall, "max_step_s": bench.max_step_s}
        if rise["time_10_90_s"] is None or fall["time_90_10_s"] is None:
            return BuckObservation(
                bench.kind, "UNKNOWN", metrics, "PH edge resolution insufficient"
            )
        return BuckObservation(bench.kind, "MEASURED", metrics)

    if bench.kind == "startup":
        t, out = _axis_and_signals(raw, ("V(out)",))
        final_region = _window(t, bench.stop_s - 0.5e-3, bench.stop_s)
        final_values = out[final_region]
        final = float(np.mean(final_values))
        if final <= 0:
            return BuckObservation(
                bench.kind, "UNKNOWN", {"final_v": final}, "output did not start"
            )
        half = len(final_values) // 2
        final_drift = abs(float(np.mean(final_values[:half]) - np.mean(final_values[half:])))
        final_stable = final_drift <= max(0.005 * final, 0.5 * float(np.ptp(final_values)))
        t10 = _crossings(t, out, 0.1 * final, True)
        t90 = _crossings(t, out, 0.9 * final, True)
        if not len(t10) or not len(t90) or t90[0] <= t10[0]:
            return BuckObservation(
                bench.kind, "UNKNOWN", {"final_v": final}, "startup 10/90% crossings missing"
            )
        _, bins = _binned_mean(t, out, float(t10[0]), float(t90[0]), 100e-6)
        drops = np.diff(bins) if len(bins) >= 2 else np.array([])
        ring_width = max(5 / float(bench.metadata["nominal_fsw_hz"]), 20e-6)
        _, tail = _binned_mean(t, out, float(t90[0]), bench.stop_s, ring_width)
        ring_status = "UNKNOWN: no resolved low-frequency post-startup oscillation"
        if len(tail) >= 9:
            centered = tail - final
            sign_changes = int(
                np.count_nonzero(np.signbit(centered[1:]) != np.signbit(centered[:-1]))
            )
            third = max(3, len(centered) // 3)
            early_span = float(np.ptp(centered[:third]))
            late_span = float(np.ptp(centered[-third:]))
            if sign_changes >= 3 and early_span > max(0.002 * final, 1e-4):
                ring_status = (
                    "DECAYING_LOW_FREQUENCY_ENVELOPE"
                    if late_span <= 0.5 * early_span
                    else "PERSISTENT_LOW_FREQUENCY_ENVELOPE"
                )
        metrics = {
            "final_v": final,
            "final_window_stable": final_stable,
            "final_half_window_drift_v": final_drift,
            "rise_10_90_s": float(t90[0] - t10[0]),
            "first_10pct_s": float(t10[0]),
            "first_90pct_s": float(t90[0]),
            "max_v": float(np.max(out)),
            "overshoot_v": float(np.max(out) - final),
            "ascent_100us_bins": len(bins),
            "monotonicity_drops_over_2pct": int(np.count_nonzero(drops < -0.02 * final)),
            "ringing_status": ring_status,
            "ringing_bin_width_s": ring_width,
        }
        if not final_stable:
            return BuckObservation(
                bench.kind, "UNKNOWN", metrics, "startup final window is not settled"
            )
        return BuckObservation(bench.kind, "MEASURED", metrics)

    t, out = _axis_and_signals(raw, ("V(out)",))
    switch_at = float(bench.metadata["switch_at_s"])
    final_start, final_end = bench.metadata["final_window_s"]
    pre = out[_window(t, switch_at - 0.3e-3, switch_at - 0.05e-3)]
    after = out[_window(t, switch_at, final_end)]
    final = out[_window(t, final_start, final_end)]
    pre_v, final_v = float(np.mean(pre)), float(np.mean(final))
    min_v = float(np.min(after))
    dip = pre_v - min_v
    half = len(final) // 2
    final_stable = abs(float(np.mean(final[:half]) - np.mean(final[half:]))) <= max(
        0.005 * abs(final_v), 0.5 * float(np.ptp(final))
    )
    width = max(5 / float(bench.metadata["nominal_fsw_hz"]), 5e-6)
    centers, envelope = _binned_mean(t, out, switch_at, final_end, width)
    recovery_s: float | None = None
    ring_status = "UNKNOWN: no resolved post-step oscillation"
    if len(envelope) >= 10 and dip > 0 and final_stable:
        trough = int(np.argmin(envelope))
        smoothed_dip = pre_v - float(envelope[trough])
        tolerance = max(0.1 * smoothed_dip, 1e-6)
        within = abs(envelope[trough:] - final_v) <= tolerance
        suffix = np.logical_and.accumulate(within[::-1])[::-1]
        recovered = np.flatnonzero(suffix)
        if len(recovered):
            recovery_s = float(centers[trough + recovered[0]] - switch_at)
        residual = envelope[trough:] - final_v
        sign_changes = int(np.count_nonzero(np.signbit(residual[1:]) != np.signbit(residual[:-1])))
        split = max(3, len(residual) // 3)
        early_amplitude = float(np.ptp(residual[:split]))
        late_amplitude = float(np.ptp(residual[-split:]))
        if sign_changes >= 3 and late_amplitude <= 0.5 * early_amplitude:
            ring_status = "DECAYING_ENVELOPE"
        elif sign_changes >= 3 and late_amplitude >= early_amplitude:
            ring_status = "PERSISTENT_ENVELOPE"
    metrics = {
        "pre_step_v": pre_v,
        "final_v": final_v,
        "minimum_v": min_v,
        "dip_v": dip,
        "final_window_stable": final_stable,
        "recovery_90pct_dip_s": recovery_s,
        "recovery_method": "5-cycle-or-5us binned VOUT; enter final +/-10% of binned dip and stay to final window",
        "ring_down_status": ring_status,
        "nominal_load_a": [bench.metadata["pre_load_a"], bench.metadata["post_load_a"]],
    }
    if not final_stable or recovery_s is None:
        return BuckObservation(
            bench.kind, "UNKNOWN", metrics, "final output or 90%-dip recovery not settled"
        )
    return BuckObservation(bench.kind, "MEASURED", metrics)


def evaluate_waveform(bench: BuckBench, raw: RawFile) -> BuckObservation:
    """Measure a real raw trace; missing resolution is UNKNOWN, never zero/PASS."""
    try:
        return _evaluate(bench, raw)
    except (KeyError, ValueError, IndexError, FloatingPointError) as exc:
        return BuckObservation(bench.kind, "UNKNOWN", {}, f"{type(exc).__name__}: {exc}")
