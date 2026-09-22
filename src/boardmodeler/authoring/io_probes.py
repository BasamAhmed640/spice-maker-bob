"""Deterministic electrical probes for buffer and level-shifter model views.

The external port contract is VCC (output supply), A, Y, GND; OE is required
only for disabled-state tests. VCCA/VCCB, when declared, use explicit rails.
These probes do not establish internal logic, protocol, or serial-channel fidelity.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from boardmodeler.authoring.probes import ProbeSpec


def _render(spec: ProbeSpec, p: Mapping[str, float], model: Path, subckt: str) -> str:
    from boardmodeler.authoring.probes import ProbeError, _deck, _fmt, model_ports

    identifier = spec.probe_id
    supply = p["io_vcc"]
    if identifier == "io_power_off_leakage" and supply != 0:
        raise ProbeError(
            "power_off_supply_invalid",
            "a power-off leakage deck is measured at VCC = 0 V; a powered condition is a "
            "different measurement",
        )
    high = identifier != "io_vol"
    input_v = p["io_input_high"] if high != bool(p["io_inverting"]) else 0.0
    enabled = identifier != "io_leakage"
    enable = p["io_vcc"] if enabled == bool(p["io_oe_active_high"]) else 0.0
    cards = [
        f"Vcc vcc 0 {_fmt(supply)}",
        f"Vcca vcca 0 {_fmt(0.0 if supply == 0 else p['io_input_high'])}",
        f"Voe oe 0 {_fmt(enable)}",
        f"Cload y 0 {_fmt(p['io_cap_f'])}",
    ]
    if identifier in ("io_delay_rise", "io_delay_fall", "io_rise_time", "io_fall_time"):
        cards.append(
            f"Va a 0 PULSE(0 {_fmt(p['io_input_high'])} {_fmt(p['io_step_s'])} "
            f"{_fmt(p['io_edge_s'])} {_fmt(p['io_edge_s'])} "
            f"{_fmt(p['io_pulse_s'])} {_fmt(4 * p['io_pulse_s'])})"
        )
        cards.append("Rload y 0 1e9")
    elif identifier in ("io_leakage", "io_power_off_leakage"):
        cards.extend([f"Va a 0 {_fmt(input_v)}", f"Vtest y 0 {_fmt(p['io_test_v'])}"])
    elif identifier == "io_input_leakage":
        cards.extend([f"Va a 0 {_fmt(p['io_test_v'])}", "Rload y 0 1e9"])
    else:
        current = p["io_load_a"] if identifier == "io_voh" else -p["io_load_a"]
        cards.extend([f"Va a 0 {_fmt(input_v)}", f"Iload y 0 {_fmt(current)}"])
    saved = ["V(a)", "V(y)", "I(Va)", "I(Vcc)"]
    if identifier in ("io_leakage", "io_power_off_leakage"):
        saved.append("I(Vtest)")
    return _deck(
        spec,
        p,
        model_lib=model,
        subckt=subckt,
        ports=model_ports(model, subckt),
        cards=cards,
        save=saved,
        port_nets={
            "VCC": "vcc",
            "VDD": "vcc",
            "VCCA": "vcca",
            "VCCB": "vcc",
            "A": "a",
            "Y": "y",
            "OE": "oe",
            "GND": "0",
        },
    )


def _cross(t, y, threshold: float, rising: bool, start: float) -> float:
    from boardmodeler.authoring.probes import ProbeError

    edges = (
        ((y[:-1] < threshold) & (y[1:] >= threshold))
        if rising
        else ((y[:-1] > threshold) & (y[1:] <= threshold))
    )
    indices = np.flatnonzero(edges)
    times = t[indices] + (threshold - y[indices]) * (t[indices + 1] - t[indices]) / (
        y[indices + 1] - y[indices]
    )
    times = times[times >= start]
    if not len(times):
        raise ProbeError("no_crossing", "the requested I/O transition was not observed")
    return float(times[0])


def _measure(identifier: str, raw: Path, p: Mapping[str, float]) -> dict[str, float]:
    from boardmodeler.authoring.probes import ProbeError, _load

    waves = _load(raw, p)
    if identifier in ("io_voh", "io_vol"):
        values = waves.y("V(y)")[waves.t >= p["tstop_s"] * 0.9]
        if not len(values):
            raise ProbeError("window_uncovered")
        if np.ptp(values) > max(1e-6, p["io_vcc"] * 0.001):
            raise ProbeError("output_not_settled", "extend the DC measurement window")
        return {"io_voltage_v": float(np.min(values) if identifier == "io_voh" else np.max(values))}
    if "leakage" in identifier:
        signal = "I(Va)" if identifier == "io_input_leakage" else "I(Vtest)"
        values = waves.y(signal)[waves.t >= p["tstop_s"] * 0.9]
        if not len(values):
            raise ProbeError("window_uncovered")
        return {"io_current_a": float(np.max(np.abs(values)))}
    rising = identifier.endswith("rise") or identifier == "io_rise_time"
    start = p["io_step_s"] - p["tmax_s"]
    if "delay" in identifier:
        if not (0 < p["io_input_frac"] < 1 and 0 < p["io_output_frac"] < 1):
            raise ProbeError("threshold_fraction_invalid")
        input_time = _cross(
            waves.t,
            waves.y("V(a)"),
            p["io_input_high"] * p["io_input_frac"],
            rising != bool(p["io_inverting"]),
            start,
        )
        output_time = _cross(
            waves.t, waves.y("V(y)"), p["io_vcc"] * p["io_output_frac"], rising, start
        )
        delta = output_time - input_time
        if delta < -p["tmax_s"]:
            raise ProbeError(
                "noncausal_transition", "output crossed before the corresponding input"
            )
        return {"io_time_s": max(0.0, delta)}
    low, high = p["io_low_frac"], p["io_high_frac"]
    if not 0 < low < high < 1:
        raise ProbeError("threshold_fraction_invalid")
    first, last = (low, high) if rising else (high, low)
    a = _cross(waves.t, waves.y("V(y)"), p["io_vcc"] * first, rising, start)
    b = _cross(waves.t, waves.y("V(y)"), p["io_vcc"] * last, rising, a)
    return {"io_time_s": b - a}


def register(registry: dict[str, ProbeSpec]) -> None:
    from boardmodeler.authoring.probes import ProbeSpec

    defaults = {
        "io_vcc": 3.3,
        "io_input_high": 3.3,
        "io_load_a": 0.002,
        "io_cap_f": 15e-12,
        "io_test_v": 3.3,
        "io_step_s": 50e-9,
        "io_edge_s": 1e-9,
        "io_pulse_s": 100e-9,
        "io_low_frac": 0.1,
        "io_high_frac": 0.9,
        "io_inverting": 0,
        "io_oe_active_high": 1,
        "io_input_frac": 0.5,
        "io_output_frac": 0.5,
        "tstop_s": 250e-9,
        "tmax_s": 0.1e-9,
    }
    for identifier, key, unit in (
        ("io_voh", "io_voltage_v", "V"),
        ("io_vol", "io_voltage_v", "V"),
        ("io_leakage", "io_current_a", "A"),
        ("io_power_off_leakage", "io_current_a", "A"),
        ("io_input_leakage", "io_current_a", "A"),
        ("io_delay_rise", "io_time_s", "s"),
        ("io_delay_fall", "io_time_s", "s"),
        ("io_rise_time", "io_time_s", "s"),
        ("io_fall_time", "io_time_s", "s"),
    ):
        ports = ("VCC", "A", "Y", "GND")
        if identifier == "io_leakage":
            ports += ("OE",)

        def measure(raw, params, probe_id=identifier):
            return _measure(probe_id, raw, params)

        probe_defaults = dict(defaults)
        if identifier == "io_power_off_leakage":
            probe_defaults["io_vcc"] = 0.0
            probe_defaults["io_input_high"] = 0.0
        registry[identifier] = ProbeSpec(
            probe_id=identifier,
            title=identifier.replace("_", " "),
            question=f"What is {key} at this explicitly declared I/O operating point?",
            unit=unit,
            ports_needed=ports,
            judge_key=key,
            defaults=probe_defaults,
            renderer=_render,
            measurer=measure,
        )
