"""Deterministic test circuits for the peak-current buck template (no planner request).

A circuit here is built from the part's own pin roles and cited rows, and it is then
checked by the same rules an AI-planned fixture must pass (``_buck_fixture_issue`` in
:mod:`boardmodeler.authoring.test_planner`). The fixture only decides *where and when*
to look; the verdict still comes from the LTspice harness against the frozen limits.

The current-limit bench measures the peak switch current through a 20 mOhm sense
resistor between PH and the inductor, with a load that asks for 1.5x the highest cited
limit, and only after the calculated soft-start reaches the highest supplied
reference voltage, plus a settling margin. A calculation from a cited SS charge
current is not itself a measured or guaranteed silicon maximum.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from boardmodeler.domain.enums import RequirementClass
from boardmodeler.domain.records import Requirement
from boardmodeler.requirements.model import UnknownUnitError, scale_factor

__all__ = [
    "complete_buck_bindings",
    "current_limit_recipe",
    "reference_voltage_recipe",
    "soft_start_time",
]

#: Settling after soft-start before the measurement window opens, and its length.
SETTLE_S = 1e-3
WINDOW_S = 2e-3
SENSE_OHMS = 0.02
SS_CAPACITANCE = 10e-9
FEEDBACK_LOWER_OHMS = 10e3


def soft_start_time(capacitance: float, charge_current: float, reference: float) -> float:
    """Seconds for the cited SS current to charge ``capacitance`` to ``reference``."""
    if min(capacitance, charge_current, reference) <= 0:
        raise ValueError("soft-start needs a positive capacitance, charge current and reference")
    return capacitance * reference / charge_current


def current_limit_recipe(
    roles: Mapping[str, str],
    ground_ties: tuple[str, ...] = (),
    *,
    vin: float,
    charge_current: float,
    reference_min: float,
    reference_typ: float,
    limit_high: float,
    evidence: str,
    reference_max: float | None = None,
    output_voltage: float = 3.3,
    inductance: float = 10e-6,
    output_capacitance: float = 22e-6,
) -> dict[str, Any]:
    """A current-limit bench for a part whose pins matched the template's roles.

    ``roles`` maps template role -> the part's terminal name (see ``match_pins``).
    ``limit_high`` is the highest cited current-limit value (maximum, else typical); the
    load demands 1.5x that so the converter must be in current limit. Supply the
    cited ``reference_max`` when one exists; otherwise the typical value is the
    highest supplied reference.
    """
    if min(vin, output_voltage, limit_high, reference_typ) <= 0 or output_voltage >= vin:
        raise ValueError("current-limit bench needs 0 < VOUT < VIN and a positive limit")
    highest_reference = reference_typ if reference_max is None else reference_max
    if highest_reference < max(reference_min, reference_typ):
        raise ValueError("current-limit bench reference maximum cannot be below min/typ")
    start = soft_start_time(SS_CAPACITANCE, charge_current, highest_reference) + SETTLE_S
    stop = start + WINDOW_S
    upper = FEEDBACK_LOWER_OHMS * (output_voltage / reference_typ - 1)
    load = output_voltage / (1.5 * limit_high)
    node = {
        "VIN": "vin",
        "EN": "en",
        "SS": "ss",
        "VSENSE": "vsense",
        "COMP": "comp",
        "PH": "ph",
        "BOOT": "boot",
        "GND": "0",
    }
    terminals = {roles[role]: node[role] for role in node}
    terminals.update({pad: "0" for pad in ground_ties})
    return {
        "purpose": "Peak switch current in current limit, measured after cited soft-start",
        "unit": "A",
        "terminals": terminals,
        "components": [
            f"V1 vin 0 {vin:g}",
            "V2 en 0 3.3",
            "Cboot boot ph 0.1u",
            "Dcatch 0 ph BM_CATCH",
            f"Rsense ph sw {SENSE_OHMS:g}",
            f"L1 sw vout {inductance:g}",
            f"C1 vout 0 {output_capacitance:g}",
            f"Rload vout 0 {load:.6g}",
            f"R1 vout vsense {upper:.6g}",
            f"R2 vsense 0 {FEEDBACK_LOWER_OHMS:g}",
            f"C2 ss 0 {SS_CAPACITANCE:g}",
            "Rcomp comp comp_c 10k",
            "Ccomp comp_c 0 1n",
        ],
        "stop": stop,
        "step": 5e-8,
        "measurement": {
            "operation": "max",
            "signal": "V(ph,sw)",
            "start": start,
            "end": stop,
            "scale": 1 / SENSE_OHMS,
        },
        "operating_point": {"VIN": vin, "VOUT_target": output_voltage, "RLOAD": load},
        "condition_evidence": (
            f"{evidence} Deterministic template bench: VIN {vin:g} V, load {load:.4g} ohm asks "
            f"for 1.5x the {limit_high:g} A limit; SS {SS_CAPACITANCE:g} F at the cited "
            f"{charge_current:g} A reaches {highest_reference:g} V at "
            f"{start - SETTLE_S:.4g} s (not a guaranteed silicon maximum), "
            f"so the peak is read from {start:.4g} s to {stop:.4g} s."
        ),
    }


def reference_voltage_recipe(
    roles: Mapping[str, str],
    ground_ties: tuple[str, ...] = (),
    *,
    vin: float,
    charge_current: float,
    reference_min: float,
    reference_typ: float,
    reference_max: float,
    load_current: float,
    evidence: str,
    output_voltage: float = 3.3,
) -> dict[str, Any]:
    """Observe the regulated feedback pin with a real power stage and resistive load."""
    if not 0 < reference_min <= reference_typ <= reference_max < output_voltage < vin:
        raise ValueError("reference bench needs ordered positive reference values below VOUT < VIN")
    if not math.isfinite(load_current) or load_current <= 0:
        raise ValueError("reference bench needs a positive finite load current")
    start = soft_start_time(SS_CAPACITANCE, charge_current, reference_max) + SETTLE_S
    stop = start + WINDOW_S
    load = output_voltage / load_current
    upper = FEEDBACK_LOWER_OHMS * (output_voltage / reference_typ - 1)
    node = {
        "VIN": "vin",
        "EN": "en",
        "SS": "ss",
        "VSENSE": "vsense",
        "COMP": "comp",
        "PH": "ph",
        "BOOT": "boot",
        "GND": "0",
    }
    terminals = {roles[role]: node[role] for role in node}
    terminals.update({pad: "0" for pad in ground_ties})
    return {
        "purpose": "Steady-state voltage reference under a resistive load",
        "unit": "V",
        "terminals": terminals,
        "components": [
            f"V1 vin 0 {vin:g}",
            "V2 en 0 3.3",
            "Cboot boot ph 0.1u",
            "Dcatch 0 ph BM_CATCH",
            "L1 ph vout 10u",
            "C1 vout 0 22u",
            f"Rload vout 0 {load:.6g}",
            f"R1 vout vsense {upper:.6g}",
            f"R2 vsense 0 {FEEDBACK_LOWER_OHMS:g}",
            f"C2 ss 0 {SS_CAPACITANCE:g}",
            "Rcomp comp comp_c 10k",
            "Ccomp comp_c 0 1n",
        ],
        "stop": stop,
        "step": 5e-8,
        "measurement": {"operation": "mean", "signal": "V(vsense)", "start": start, "end": stop},
        "operating_point": {"VIN": vin, "VOUT_target": output_voltage, "RLOAD": load},
        "condition_evidence": (
            f"{evidence} Code-built bench at VIN {vin:g} V, selected {output_voltage:g} V "
            f"output and {load_current:g} A resistive load. The feedback pin is observed, never "
            f"forced. SS {SS_CAPACITANCE:g} F charges at the cited {charge_current:g} A to "
            f"{reference_max:g} V, then settles for {SETTLE_S:g} s; measurement starts at "
            f"{start:g} s. This estimate is not a guaranteed silicon maximum. Passives and "
            "EN=3.3 V are selected test equipment; nominal 25 C only."
        ),
    }


_REFERENCE = re.compile(r"\b(?:voltage\s+reference|reference\s+voltage)\b", re.I)
_CURRENT_LIMIT = re.compile(r"\b(?:switch\s+)?current[- ]limit\b", re.I)
_SS_CURRENT = re.compile(r"(?:slow|soft)[ -]?start.*\bcharge\s+current\b", re.I)
# A table header's VIN range is not a sampled operating point. Keep explicit
# singleton conditions, and let the caller reject contradictory point values.
_VIN = re.compile(r"\bVIN\s*=\s*(\d+(?:\.\d+)?)\s*V\b(?!\s*(?:to\b|[-\u2013\u2014])\s*\d)", re.I)


def _positive_limits(row: Requirement, unit: str) -> dict[str, float]:
    if row.limits is None:
        return {}
    try:
        factor = scale_factor(row.limits.unit, unit)
    except ValueError, UnknownUnitError:
        return {}
    return {
        side: number
        for side in ("min", "typ", "max")
        if (value := getattr(row.limits, side)) is not None
        and math.isfinite(number := float(value) * factor)
        and number > 0
    }


def complete_buck_bindings(
    requirements: Sequence[Requirement],
    pin_map: Sequence[dict[str, Any]],
    entries: Sequence[dict[str, Any]],
    *,
    unverified: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Fill two physical buck tests from raw cited rows, before any spec is frozen.

    Existing circuit recipes are kept. Generic reduced-model probes may be replaced
    for this physical pin family. Callers must not apply this to supplied/frozen bindings.
    No candidate design, parameter default, library or simulation result is read.
    """
    from boardmodeler.authoring.pin_roles import physical_terminals
    from boardmodeler.authoring.test_planner import validate_partial_plan
    from boardmodeler.models.buck_switching import match_pins

    result = [dict(entry) for entry in entries]
    try:
        ports = physical_terminals(pin_map)
        pins = match_pins(ports)
    except KeyError, ValueError:
        return result
    if not pins.ok:
        return result
    verified = [
        row
        for row in requirements
        if row.citation_verified
        and row.req_id not in unverified
        and row.evidence
        and row.req_class not in (RequirementClass.UNKNOWN, RequirementClass.ABSOLUTE_MAXIMUM)
        and row.status == "active"
        and not row.conflicts
    ]
    by_id = {row.req_id: row for row in verified}
    for index, entry in enumerate(result):
        row = by_id.get(entry["req_id"])
        if row is None or entry.get("probe") not in (None, "vref", "current_limit"):
            continue
        reference = bool(_REFERENCE.search(row.statement)) and bool(_positive_limits(row, "V"))
        limit = bool(_CURRENT_LIMIT.search(row.statement)) and bool(_positive_limits(row, "A"))
        if not reference and not limit:
            continue
        docs = {e.doc_id for e in row.evidence}
        related = [
            candidate
            for candidate in verified
            if candidate.applies_to == row.applies_to
            and docs & {e.doc_id for e in candidate.evidence}
        ]
        references = [
            r for r in related if _REFERENCE.search(r.statement) and _positive_limits(r, "V")
        ]
        limits = [
            r for r in related if _CURRENT_LIMIT.search(r.statement) and _positive_limits(r, "A")
        ]
        charging = [
            r for r in related if _SS_CURRENT.search(r.statement) and _positive_limits(r, "A")
        ]
        if not references or not limits or not charging:
            continue
        ref_values = [value for r in references for value in _positive_limits(r, "V").values()]
        typical = {
            _positive_limits(r, "V")["typ"] for r in references if "typ" in _positive_limits(r, "V")
        }
        if len(typical) > 1:
            continue
        ref_min, ref_max = min(ref_values), max(ref_values)
        ref_typ = next(iter(typical)) if typical else (ref_min + ref_max) / 2
        currents = [_positive_limits(r, "A") for r in limits]
        high = [values.get("max", values.get("typ")) for values in currents]
        high = [value for value in high if value is not None]
        low = [values.get("min", values.get("typ")) for values in currents]
        low = [value for value in low if value is not None]
        if not high or not low:
            continue
        charge = min(
            values.get("min", values.get("typ", math.inf))
            for r in charging
            if (values := _positive_limits(r, "A"))
        )
        direct_vin = {
            float(m[1])
            for text in (row.statement, *(c.text for c in row.conditions))
            for m in _VIN.finditer(text)
        }
        vin_values = direct_vin or {
            float(m[1])
            for r in (*references, *limits)
            for text in (r.statement, *(c.text for c in r.conditions))
            for m in _VIN.finditer(text)
        }
        if len(vin_values) != 1 or not math.isfinite(charge):
            continue
        vin = next(iter(vin_values))
        evidence = (
            "Independent raw citations: "
            + "; ".join(
                f"{r.req_id} ({', '.join(f'{e.doc_id} page {e.page.pdf_page}' for e in r.evidence)})"
                for r in (*references, *limits, *charging)
            )
            + "."
        )
        common = dict(
            vin=vin,
            charge_current=charge,
            reference_min=ref_min,
            reference_typ=ref_typ,
            reference_max=ref_max,
            evidence=evidence,
        )
        try:
            recipe = (
                reference_voltage_recipe(
                    pins.roles, pins.ground_ties, load_current=min(low) / 4, **common
                )
                if reference
                else current_limit_recipe(
                    pins.roles, pins.ground_ties, limit_high=max(high), **common
                )
            )
            checked = validate_partial_plan(
                {
                    "bindings": [
                        {"req_id": row.req_id, "probe": "circuit_measurement", "recipe": recipe}
                    ]
                },
                [row],
                ports,
                unverified,
                pin_map,
                context=[r.model_dump(mode="json", by_alias=True) for r in related],
            )
        except ValueError, TypeError, KeyError:
            continue
        if checked[0].get("probe"):
            result[index] = checked[0]
    return result
