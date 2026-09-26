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

from collections.abc import Mapping
from typing import Any

__all__ = ["current_limit_recipe", "soft_start_time"]

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
