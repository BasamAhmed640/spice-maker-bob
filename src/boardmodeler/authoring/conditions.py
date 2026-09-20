"""Compile explicit datasheet operating points without inventing conditions."""

from __future__ import annotations

import math
import re

ALIASES = {
    "vin": "vin_dc",
    "vcc": "vin_dc",
    "vdd": "vin_dc",
    "vout": "vout_nom",
    "cl": "c_out",
    "cout": "c_out",
    "rl": "r_load",
    "rload": "r_load",
    "ven": "en_high",
    "ta": "temp_c",
    "tj": "temp_c",
    "temperature": "temp_c",
}
_UNITS = {
    "vin": "v",
    "vcc": "v",
    "vdd": "v",
    "vout": "v",
    "ven": "v",
    "cl": "f",
    "cout": "f",
    "rl": "ohm",
    "rload": "ohm",
    "ta": "c",
    "tj": "c",
    "ioh": "a",
    "iol": "a",
}
_VALUE = re.compile(
    r"\b(VIN|VCC|VDD|VOUT|VEN|CL|COUT|RL|RLOAD|TA|TJ|IOH|IOL)\s*=\s*"
    r"([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)\s*([pnumkµμ]?)(V|A|F|ohm|°?C)\b"
    r"(?!\s*(?:to|[\u2013\u2212-])\s*[-+]?\d)",
    re.IGNORECASE,
)
_SCALES = {"": 1, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3}


def operating_params(requirement, probe) -> tuple[dict[str, float], str | None]:
    allowed = probe.merged_params({})
    aliases = dict(ALIASES)
    if probe.probe_id.startswith("io_"):
        aliases.update(
            {
                "vcc": "io_vcc",
                "vdd": "io_vcc",
                "cl": "io_cap_f",
                "vin": "io_input_high",
                "ioh": "io_load_a",
                "iol": "io_load_a",
            }
        )
    params = {}
    bounds = []
    for condition in requirement.conditions:
        values = {}
        for match in _VALUE.finditer(condition.text):
            name, value, prefix, unit = match.groups()
            if unit.lower().lstrip("°") != _UNITS[name.lower()]:
                return {}, f"condition_unit_invalid: {name} cannot use {unit}"
            key = aliases.get(name.lower(), name.lower())
            number = float(value) * _SCALES[prefix.lower()]
            if name.lower() in ("ioh", "iol"):
                number = abs(number)
            values[key] = number
        for key, value in condition.parameter_overrides.items():
            bound = re.fullmatch(r"(.+)_(min|max)", key, re.IGNORECASE)
            if bound:
                base = aliases.get(bound[1].lower(), bound[1])
                bounds.append((base, bound[2].lower(), value, key))
                continue
            mapped = aliases.get(key.lower(), key)
            number = float(value)
            if key.lower() in ("ioh", "iol"):
                number = abs(number)
            if mapped in values and values[mapped] != number:
                return {}, f"condition_conflict: {mapped} disagrees with the cited operating point"
            values[mapped] = number
        for key, value in values.items():
            if key not in allowed or not math.isfinite(value):
                return {}, f"condition_unsupported: {key} cannot be applied by {probe.probe_id}"
            if key in params and params[key] != value:
                return (
                    {},
                    f"condition_conflict: multiple values for {key}; split the datasheet rows",
                )
            if key == "temp_c" and value != 25:
                return (
                    {},
                    "temperature_dependence_unverified: only nominal 25 C behavior is supported",
                )
            params[key] = value
    for base, side, value, key in bounds:
        nominal = params.get(base, allowed.get(base))
        if nominal is None or not math.isfinite(value):
            return {}, f"condition_unsupported: {key} has no nominal operating point"
        if (side == "min" and nominal < value) or (side == "max" and nominal > value):
            return (
                {},
                f"condition_outside_range: nominal {base}={nominal:g} violates {key}={value:g}",
            )
    if probe.probe_id.startswith("io_"):
        required = {"io_vcc"}
        if probe.probe_id in ("io_voh", "io_vol"):
            required.add("io_load_a")
        if "delay" in probe.probe_id or probe.probe_id in ("io_rise_time", "io_fall_time"):
            required.update(("io_cap_f", "io_edge_s"))
        if probe.probe_id in ("io_rise_time", "io_fall_time"):
            required.update(("io_low_frac", "io_high_frac"))
        if "delay" in probe.probe_id:
            required.update(("io_input_frac", "io_output_frac"))
        if "leakage" in probe.probe_id:
            required.add("io_test_v")
        else:
            required.add("io_inverting")
        if probe.probe_id == "io_leakage":
            required.add("io_oe_active_high")
        missing = required - params.keys()
        if missing:
            return {}, "condition_missing: I/O measurement needs " + ", ".join(sorted(missing))
        params.setdefault("io_input_high", params["io_vcc"])
        for key in ("io_vcc", "io_input_high", "io_cap_f", "io_load_a"):
            if key in params and params[key] <= 0:
                return {}, f"condition_invalid: {key} must be positive"
        for key in ("io_inverting", "io_oe_active_high"):
            if key in params and params[key] not in (0, 1):
                return {}, f"condition_invalid: {key} must be 0 or 1"
    return params, None
