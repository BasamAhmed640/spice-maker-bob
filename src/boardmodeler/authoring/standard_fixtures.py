"""Small, reviewed fixture compilers for well-defined datasheet quantities.

These select conditions from extracted evidence, never from a part-number list.
An ambiguous pin map or missing condition leaves the row for the general planner.
"""

from __future__ import annotations

import re

from boardmodeler.authoring.circuit_probe import CircuitRecipe
from boardmodeler.authoring.pin_roles import terminal_name


def declared_delay_threshold(payload, req):
    """Use an explicitly recorded shared VM for a missing/zero delay threshold.

    Older planners omitted ``level`` and the schema defaulted it to zero.
    This happens before freezing; it never changes limits or a running fixture.
    Without a stated VM, an omitted threshold is rejected by Measurement.
    """
    m = payload.get("measurement", {})
    if m.get("operation") != "delay" or m.get("level") not in (None, 0):
        return payload
    source = " ".join([req.statement, *(c.text for c in req.conditions)])
    values = {float(v) for v in re.findall(r"\bV\s*M\s*=\s*([\d.]+)\s*V\b", source, re.I)}
    if not values and re.search(r"\bV\s*M\s*=\s*V\s*CC\s*/\s*2\b", source, re.I):
        supply = payload.get("operating_point", {}).get("VCC")
        if isinstance(supply, (int, float)) and supply > 0:
            values.add(supply / 2)
    if len(values) != 1:
        return payload
    level = values.pop()
    if level <= 0 or abs(m.get("trigger_level", 0) - level) > 1e-8:
        return payload
    return {
        **payload,
        "measurement": {**m, "level": level},
        "condition_evidence": payload.get("condition_evidence", "")
        + f" Output threshold compiled from the same explicitly declared VM={level:g} V as the input threshold.",
    }


def opamp_output_swing(req, pin_map, requirements=()):
    text = req.statement
    if not re.search(r"output swing.*(?:supply )?rails", text, re.I):
        return None
    # A load to ground is a different test. Require the common table condition
    # from this row or another row on the same cited page before compiling it.
    pages = {e.page.pdf_page for e in req.evidence}
    related = [req, *(r for r in requirements if pages & {e.page.pdf_page for e in r.evidence})]
    conditions = " ".join(c.text for r in related for c in r.conditions)
    if not re.search(r"connected\s+to\s+V\s*S\s*/\s*2", conditions, re.I):
        return None
    supply = re.search(r"V\s*S\s*=\s*([\d.]+)\s*V", text, re.I)
    load = re.search(r"R\s*L\s*=\s*([\d.]+)\s*([kK]?)\s*(?:Ω|ohm)", text)
    if not supply or not load:
        return None
    vcc = float(supply[1])
    resistance = float(load[1]) * (1000 if load[2] else 1)
    if vcc <= 0 or resistance <= 0:
        return None
    terminals, channels = {}, {}
    power = set()
    for pin in pin_map:
        function = pin.get("function", "").lower().replace("-", "")
        name = terminal_name(pin)
        channel = re.search(r"channel\s*(\d+)", function)
        index = channel[1] if channel else "1"
        if pin.get("direction") == "nc":
            terminals[name] = "nc_" + name
        elif "positive" in function and "supply" in function:
            terminals[name] = "vcc"
            power.add("positive")
        elif "negative" in function and ("supply" in function or "ground" in function):
            terminals[name] = "0"
            power.add("negative")
        elif function.startswith("noninverting"):
            terminals[name] = "drive"
            channels.setdefault(index, set()).add("plus")
        elif function.startswith("inverting"):
            terminals[name] = "mid"
            channels.setdefault(index, set()).add("minus")
        elif function.startswith("output") and pin.get("direction") == "output":
            terminals[name] = "out" + index
            channels.setdefault(index, set()).add("output")
        else:
            return None
    if (
        power != {"positive", "negative"}
        or not channels
        or any(v != {"plus", "minus", "output"} for v in channels.values())
    ):
        return None
    outputs = ["out" + key for key in sorted(channels)]
    data = {
        "purpose": "Both-rail output swing, every amplifier channel",
        "unit": "V",
        "terminals": terminals,
        "components": [
            f"Vcc vcc 0 {vcc:g}",
            f"Vmid mid 0 {vcc / 2:g}",
            f"Vin drive 0 PWL(0 {vcc / 2 - 0.1:g} 0.002 {vcc / 2 - 0.1:g} 0.002001 {vcc / 2 + 0.1:g} 0.004 {vcc / 2 + 0.1:g})",
            *(f"Rload{i} {node} mid {resistance:g}" for i, node in enumerate(outputs)),
        ],
        "stop": 0.004,
        "step": 1e-6,
        "measurement": {
            "operation": "rail_headroom",
            "signal": f"V({outputs[0]})",
            "additional_signals": [f"V({n})" for n in outputs[1:]],
            "reference": "V(vcc)",
            "start": 0.0015,
            "end": 0.0019,
            "second_start": 0.0035,
            "second_end": 0.0039,
        },
        "operating_point": {"VS": vcc, "RL": resistance},
        "condition_evidence": "Supply and load from the cited output-swing row. Each channel is driven to both rails with input common mode near mid-supply; load is referenced to mid-supply. Nominal 25 C only.",
    }
    return CircuitRecipe.model_validate(data).model_dump(mode="json")
