"""Observed DC, AC and transient checks for an eight-pin dual op amp.

The second amplifier is always terminated as a follower, never left floating.
AC measurements inject a series feedback voltage and calculate output divided
by the observed differential input, preserving a stable DC operating point.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import numpy as np

PORTS = ("OUT1", "IN1M", "IN1P", "VEE", "IN2P", "IN2M", "OUT2", "VCC")


def render(spec, p, model, subckt):
    from boardmodeler.authoring.probes import ProbeError, model_ports

    if p["op_channel"] not in (1, 2) or p["op_vcc"] <= 0 or p["op_load"] <= 0:
        raise ProbeError("opamp_conditions_invalid")
    kind = spec.probe_id.removeprefix("opamp_")
    active = ("out", "inm", "inp")
    other = ("other", "other", "ref")
    first, second = (active, other) if p["op_channel"] == 1 else (other, active)
    mapping = dict(zip(PORTS, (*first, "0", second[2], second[1], second[0], "vcc"), strict=True))
    ports = model_ports(model, subckt)
    if {port.upper() for port in ports} != set(PORTS):
        raise ProbeError("opamp_pin_contract", "expected the eight named dual-amplifier pins")
    cards = [
        f"Vcc vcc 0 {p['op_vcc']:g}",
        f"Vref ref 0 {p['op_vcc'] / 2:g}",
        f"Xamp {' '.join(mapping[port.upper()] for port in ports)} {subckt}",
        f"Rload out 0 {p['op_load']:g}",
    ]
    save = ["V(inp)", "V(inm)", "V(out)", "I(Vcc)"]
    if kind in ("gain", "gbw"):
        cards += [
            "Vin inp 0 DC 0 AC 0",
            f"Vtest inm out DC {-p['op_vout']:g} AC 1",
            ".ac dec 80 1 100Meg",
        ]
    else:
        if kind in ("offset", "bias", "offset_current"):
            cards += [
                "Vin inp 0 0",
                f"Bservo servo 0 V=limit(1e4*(V(out)-{p['op_vout']:g}),-0.1,{p['op_vcc'] - 1.5:g})",
                "Vsense inm servo 0",
            ]
            save += ["I(Vin)", "I(Vsense)"]
        elif kind in ("slew_rise", "slew_fall"):
            cards += [
                "Vin inp 0 PULSE(1 3 20u 1n 1n 40u 100u)",
                "Vfeedback inm out 0",
            ]
        elif kind in ("swing_high", "swing_low"):
            cards += [
                f"Vin inp 0 {1 if kind == 'swing_high' else 0}",
                f"Vminus inm 0 {0 if kind == 'swing_high' else 1}",
            ]
        else:
            cards += [f"Vin inp 0 {p['op_input']:g}", "Vfeedback inm out 0"]
        cards.append(f".tran 0 {p['tstop_s']:g} 0 {p['tmax_s']:g}")
    return "\n".join(
        [
            f"* {spec.title}; channel {p['op_channel']:g}",
            f'.include "{model.resolve().as_posix()}"',
            *cards,
            f".temp {p['temp_c']:g}",
            ".options plotwinsize=0 numdgt=15 method=gear gminsteps=0 srcsteps=0",
            ".save " + " ".join(save),
            ".end",
            "",
        ]
    )


def measure(kind: str, raw_path: Path, p):
    from boardmodeler.authoring.probes import ProbeError, _load
    from boardmodeler.simulation.raw import read_raw

    if kind in ("gain", "gbw"):
        raw = read_raw(raw_path)
        if not raw.complex_data or raw.variables[0].lower() != "frequency":
            raise ProbeError("opamp_ac_missing")
        f = raw.data[:, 0].real
        differential = raw.column("V(inp)") - raw.column("V(inm)")
        if np.any(np.abs(differential) < 1e-15):
            raise ProbeError("opamp_ac_input_missing")
        gain = np.abs(raw.column("V(out)") / differential)
        if len(f) < 3 or not np.all(np.isfinite(gain)) or not np.all(np.diff(f) > 0):
            raise ProbeError("opamp_ac_invalid")
        if f[0] > 1.001 or f[-1] < 99e6:
            raise ProbeError("opamp_ac_truncated")
        if kind == "gain":
            return {"opamp_value": float(gain[0])}
        edges = np.flatnonzero((gain[:-1] >= 1) & (gain[1:] < 1))
        if not len(edges):
            raise ProbeError("opamp_unity_crossing_missing")
        i = int(edges[0])
        weight = -np.log(gain[i]) / np.log(gain[i + 1] / gain[i])
        return {"opamp_value": float(np.exp(np.log(f[i]) + weight * np.log(f[i + 1] / f[i])))}
    w = _load(raw_path, p)
    end = w.t >= p["tstop_s"] * 0.9
    output = w.y("V(out)")
    if kind in ("slew_rise", "slew_fall"):
        from boardmodeler.authoring.io_probes import _cross

        rising = kind == "slew_rise"
        low, high = (1.4, 2.6) if rising else (2.6, 1.4)
        start = 20e-6 if rising else 60e-6
        a = _cross(w.t, output, low, rising, start)
        b = _cross(w.t, output, high, rising, a)
        if b <= a:
            raise ProbeError("opamp_slew_invalid")
        value = 1.2 / (b - a)
    else:
        if not np.any(end) or np.ptp(output[end]) > 1e-4:
            raise ProbeError("opamp_not_settled")
        if kind in ("offset", "bias", "offset_current"):
            if abs(float(np.mean(output[end])) - p["op_vout"]) > 0.001:
                raise ProbeError("opamp_servo_failed")
            if kind == "offset":
                value = abs(float(np.mean(w.y("V(inp)")[end] - w.y("V(inm)")[end])))
            else:
                a, b = np.mean(w.y("I(Vin)")[end]), np.mean(w.y("I(Vsense)")[end])
                if kind == "bias" and (a < 0 or b < 0):
                    raise ProbeError(
                        "opamp_bias_polarity", "expected current flowing out of the input pins"
                    )
                value = abs(float((a + b) / 2 if kind == "bias" else a - b))
        elif kind == "quiescent":
            value = abs(float(np.mean(w.y("I(Vcc)")[end]))) / 2
        elif kind == "follower":
            value = float(np.max(np.abs(output[end] - w.y("V(inp)")[end])))
        elif kind == "swing_high":
            value = p["op_vcc"] - float(np.mean(output[end]))
        else:
            value = float(np.mean(output[end]))
    return {"opamp_value": value}


def register(registry):
    from boardmodeler.authoring.probes import ProbeSpec

    defaults = {
        "op_channel": 1,
        "op_vcc": 5,
        "op_vout": 1.4,
        "op_input": 2.5,
        "op_load": 1e12,
        "temp_c": 25,
        "tstop_s": 100e-6,
        "tmax_s": 10e-9,
    }
    for kind, unit in (
        ("offset", "V"),
        ("bias", "A"),
        ("offset_current", "A"),
        ("gain", "V/V"),
        ("gbw", "Hz"),
        ("slew_rise", "V/s"),
        ("slew_fall", "V/s"),
        ("quiescent", "A"),
        ("swing_high", "V"),
        ("swing_low", "V"),
        ("follower", "V"),
    ):
        name = "opamp_" + kind
        registry[name] = ProbeSpec(
            probe_id=name,
            title="Dual op amp " + kind.replace("_", " "),
            question="What is the observed "
            + kind.replace("_", " ")
            + " at the recorded operating point?",
            unit=unit,
            ports_needed=PORTS,
            defaults=defaults,
            judge_key="opamp_value",
            renderer=render,
            measurer=partial(measure, kind),
            analysis="ac" if kind in ("gain", "gbw") else "tran",
        )
