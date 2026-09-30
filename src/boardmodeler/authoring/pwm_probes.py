"""Independent, bounded physical checks for a first-order alternating PWM controller.

All stimuli and measurement windows are reviewed fixture constants. This module
never imports candidate design parameters. Limits remain frozen datasheet rows.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import numpy as np

PORTS = (
    "VSENSE",
    "RT",
    "RAMP_CS",
    "ILIM",
    "EN",
    "OVP_OTP",
    "VREF",
    "REF_EAP",
    "FB_EAM",
    "COMP",
    "GND",
    "VDD",
    "SRB",
    "SRA",
    "OUTB",
    "OUTA",
    "HICC",
    "PS",
    "SP",
    "SS",
)
KINDS = (
    ("vref", "V"),
    ("frequency", "Hz"),
    ("uvlo_rise", "V"),
    ("uvlo_fall", "V"),
    ("enable", "V"),
    ("ss_charge", "A"),
    ("ilim", "V"),
    ("ilim_delay", "s"),
    ("ovp", "V"),
    ("alternating", "ratio"),
    ("duty_response", "ratio"),
    ("source_resistance", "ohm"),
    ("sink_resistance", "ohm"),
    ("sp_delay", "s"),
    ("ps_delay", "s"),
)
UVLO_HOLD = 75e-6
# Plateaus are independent electrical-table endpoints and a fixed 50 mV ladder.
# The below/above-endpoint guards expose faults that output-startup latency hid
# when VDD was swept continuously. A straddling interval is UNKNOWN, never PASS.
UVLO_LEVELS = {
    "uvlo_rise": (0.0, 3.8, 3.995, *[round(4.0 + i * 0.05, 3) for i in range(14)], 4.655, 4.8),
    "uvlo_fall": (4.8, 4.405, *[round(4.4 - i * 0.05, 3) for i in range(13)], 3.795, 3.6),
}
THRESHOLD_LEVELS = {
    **UVLO_LEVELS,
    "enable": (0.0, 1.495, *[round(1.5 + i * 0.05, 3) for i in range(16)], 2.255, 3.25),
    "ovp": (0.5, 0.6595, *[round(0.66 + i * 0.01, 3) for i in range(9)], 0.7405, 0.9),
    "ilim": (0.3, 0.4965, *[round(0.497 + i * 0.001, 4) for i in range(17)], 0.5135, 0.7),
}
THRESHOLD_INPUTS = {
    "uvlo_rise": "V(vdd)",
    "uvlo_fall": "V(vdd)",
    "enable": "V(en)",
    "ovp": "V(ovp_otp)",
    "ilim": "V(ilim)",
}
THRESHOLD_BOUNDS = {
    "uvlo_rise": (4.0, 4.65),
    "uvlo_fall": (3.8, 4.4),
    "enable": (1.5, 2.25),
    "ovp": (0.66, 0.74),
    "ilim": (0.497, 0.513),
}


def _plateau_source(levels):
    points = ["0", f"{levels[0]:.12g}"]
    for index, value in enumerate(levels[1:], 1):
        boundary = index * UVLO_HOLD
        points.extend(
            (
                f"{boundary:.12g}",
                f"{levels[index - 1]:.12g}",
                f"{boundary + 1e-9:.12g}",
                f"{value:.12g}",
            )
        )
    points.extend((f"{len(levels) * UVLO_HOLD:.12g}", f"{levels[-1]:.12g}"))
    return "PWL(" + " ".join(points) + ")"


def render(spec, p, model, subckt):
    from boardmodeler.authoring.probes import ProbeError, model_ports

    kind = spec.probe_id.removeprefix("pwm_")
    expected_stop = len(THRESHOLD_LEVELS[kind]) * UVLO_HOLD if kind in THRESHOLD_LEVELS else 400e-6
    if p["temp_c"] != 25 or p["tstop_s"] != expected_stop or p["tmax_s"] > 20e-9:
        raise ProbeError(
            "pwm_fixture_domain", "reviewed 25 C, fixed-duration controller fixture required"
        )
    if kind == "ilim_delay" and p["tmax_s"] > 1e-9:
        raise ProbeError("pwm_ilim_delay_time_resolution")
    ports = model_ports(model, subckt)
    if len(ports) != 20 or {pin.upper() for pin in ports} != set(PORTS):
        raise ProbeError("pwm_fixture_pin_contract")
    mapping = {port: port.lower() for port in PORTS}
    mapping.update({"GND": "0", "FB_EAM": "comp", "VSENSE": "vref"})
    vdd = "12"
    en = "3.25"
    ilim = "0"
    ovp = "0"
    command = "1.0"
    if kind in UVLO_LEVELS:
        vdd = _plateau_source(UVLO_LEVELS[kind])
    elif kind == "enable":
        en = _plateau_source(THRESHOLD_LEVELS[kind])
    elif kind == "ilim":
        ilim = _plateau_source(THRESHOLD_LEVELS[kind])
    elif kind == "ovp":
        ovp = _plateau_source(THRESHOLD_LEVELS[kind])
    elif kind == "duty_response":
        command = "PWL(0 0.7 200u 0.7 205u 1.6 400u 1.6)"
    cards = [
        f"Vdd vdd 0 {vdd}",
        f"Ven en 0 {en}",
        f"Vilim ilim 0 {ilim}",
        f"Vovp ovp_otp 0 {ovp}",
        f"Vcommand ref_eap 0 {command}",
        "Rrt rt 0 75k",
        "Rsp sp 0 20k",
        "Rps ps 0 27k",
        "Chicc hicc 0 100n",
        "Cvdd vdd 0 1u",
        "Cvref vref 0 1u",
        "Xcontroller " + " ".join(mapping[port.upper()] for port in ports) + " " + subckt,
        "Rref vref 0 1625",  # exactly 2 mA at the nominal 3.25-V table point
        *[f"C{pin} {pin} 0 100p" for pin in ("outa", "outb", "sra", "srb")],
    ]
    if kind == "ilim_delay":
        cards[2] = "Bilim ilim 0 V=if(delay(V(outa),1u)>6,0.7,0.3)"
    if kind == "ss_charge":
        cards.append("Vss ss 0 0")
    else:
        cards.append("Css ss 0 1n")
    if kind in {"ilim", "ilim_delay"}:
        cards.append("Vramp ramp_cs 0 0.1")
    else:
        cards.extend(["Rramp vref ramp_cs 15k", "Cramp ramp_cs 0 330p"])
    if kind == "source_resistance":
        cards.append("Iload outa 0 20m")
    elif kind == "sink_resistance":
        cards.append("Iload 0 outa 20m")
    saves = "V(vdd) V(en) V(vref) V(ss) V(comp) V(ref_eap) V(ramp_cs) V(ilim) V(ovp_otp) V(outa) V(outb) V(sra) V(srb) I(Vdd)"
    if kind == "ss_charge":
        saves += " I(Vss)"
    if kind in THRESHOLD_LEVELS:
        saves = THRESHOLD_INPUTS[kind] + " V(outa) V(outb)"
    if kind == "ilim_delay":
        saves = "V(ilim) V(outa) V(outb)"
    return "\n".join(
        [
            f"* Independent UCC28251 fixture: {kind}",
            f'.include "{model.resolve().as_posix()}"',
            *cards,
            f".tran 0 {p['tstop_s']:.12g} 0 {p['tmax_s']:.12g}",
            ".temp 25",
            ".options plotwinsize=0 numdgt=15 method=gear",
            ".save " + saves,
            ".end",
            "",
        ]
    )


def _edges(t, y, rising=True, threshold=6.0):
    above = y > threshold
    indices = np.flatnonzero((~above[:-1] & above[1:]) if rising else (above[:-1] & ~above[1:]))
    return np.array(
        [float(t[i] + (threshold - y[i]) * (t[i + 1] - t[i]) / (y[i + 1] - y[i])) for i in indices]
    )


def _pulses(t, y):
    rises, falls = _edges(t, y), _edges(t, y, False)
    return [
        (rise, float(falls[np.searchsorted(falls, rise)]))
        for rise in rises
        if np.searchsorted(falls, rise) < len(falls)
    ]


def _mean(t, y, lo, hi):
    mask = (t >= lo) & (t <= hi)
    if np.count_nonzero(mask) < 3:
        from boardmodeler.authoring.probes import ProbeError

        raise ProbeError("pwm_measurement_window_missing")
    return float(np.trapezoid(y[mask], t[mask]) / (t[mask][-1] - t[mask][0]))


def measure(kind: str, raw_path: Path, p):
    from boardmodeler.authoring.probes import ProbeError, _load

    w = _load(raw_path, p)
    t = w.t
    ya, yb = w.y("V(outa)"), w.y("V(outb)")
    if kind == "ilim_delay":
        if (p["delay_min_s"], p["delay_max_s"]) not in {(12.3e-9, 38.7e-9), (15e-9, 36e-9)}:
            raise ProbeError("pwm_ilim_delay_source_bounds")
        input_edges = _edges(t, w.y("V(ilim)"), threshold=0.5)
        output_falls = _edges(t, ya, False)
        delays = []
        for edge in input_edges[(input_edges >= 200e-6) & (input_edges <= 350e-6)]:
            if np.interp(edge, t, ya) < 6:
                raise ProbeError("pwm_ilim_delay_step_outside_primary_pulse")
            i = np.searchsorted(output_falls, edge)
            if i < len(output_falls):
                delays.append(float(output_falls[i] - edge))
        if len(delays) < 8 or not np.all(np.asarray(delays) > 0):
            raise ProbeError("pwm_ilim_delay_response_missing")
        value = float(np.median(delays))
        lo, hi = value - 2 * p["tmax_s"], value + 2 * p["tmax_s"]
        if lo < p["delay_min_s"] <= hi or lo <= p["delay_max_s"] < hi:
            raise ProbeError("pwm_ilim_delay_interval_straddles_limit")
        return {"pwm_value": value, "observed_interval_low_s": lo, "observed_interval_high_s": hi}
    if kind in THRESHOLD_LEVELS:
        levels = THRESHOLD_LEVELS[kind]
        prefix = "pwm_uvlo" if kind in UVLO_LEVELS else "pwm_" + kind
        turns_on = kind in {"uvlo_rise", "enable"}
        states = []
        observed_levels = []
        ilim_pulses = sorted(_pulses(t, ya) + _pulses(t, yb)) if kind == "ilim" else []
        for index, level in enumerate(levels):
            # Startup, SS and oscillator latency settle before this fixed window.
            mask = (t >= (index + 1) * UVLO_HOLD - 25e-6) & (t <= (index + 1) * UVLO_HOLD - 1e-6)
            if (
                np.count_nonzero(mask) < 3
                or np.max(np.abs(w.y(THRESHOLD_INPUTS[kind])[mask] - level)) > 1e-4
            ):
                raise ProbeError(prefix + "_plateau_missing")
            actual = w.y(THRESHOLD_INPUTS[kind])[mask]
            observed_levels.append((float(np.min(actual)), float(np.max(actual))))
            if kind == "ilim":
                widths = [
                    end - start
                    for start, end in ilim_pulses
                    if start >= (index + 1) * UVLO_HOLD - 25e-6
                    and end <= (index + 1) * UVLO_HOLD - 1e-6
                ]
                if len(widths) < 3:
                    raise ProbeError("pwm_ilim_settled_pulses_missing")
                # Source-timed normal pulses in the fixed low-ramp fixture are
                # long; current limiting produces blank-limited short pulses.
                # These physical guards never scale with candidate pulse width.
                if max(widths) < 250e-9:
                    states.append(True)
                elif min(widths) > 1e-6:
                    states.append(False)
                else:
                    raise ProbeError("pwm_ilim_plateau_response_ambiguous")
            else:
                states.append(bool(np.max(np.maximum(ya[mask], yb[mask])) > 2.0))
        expected_first = False if kind == "ilim" else not turns_on
        expected_last = True if kind == "ilim" else turns_on
        if states[0] != expected_first or states[-1] != expected_last:
            raise ProbeError(prefix + "_endpoint_state_missing")
        transitions = [i for i in range(1, len(states)) if states[i] != states[i - 1]]
        if len(transitions) != 1:
            raise ProbeError(prefix + "_nonmonotonic")
        i = transitions[0]
        lo = min(observed_levels[i - 1][0], observed_levels[i][0])
        hi = max(observed_levels[i - 1][1], observed_levels[i][1])
        bound_lo, bound_hi = THRESHOLD_BOUNDS[kind]
        if lo < bound_lo <= hi or lo <= bound_hi < hi:
            raise ProbeError(
                prefix + "_threshold_interval_straddles_limit",
                f"Observed threshold bracket [{lo:g}, {hi:g}] V crosses a fixed table bound",
            )
        return {
            "pwm_value": (lo + hi) / 2,
            "observed_interval_low_v": lo,
            "observed_interval_high_v": hi,
        }
    pulses_a, pulses_b = _pulses(t, ya), _pulses(t, yb)
    steady = [(a, b) for a, b in pulses_a if 200e-6 <= a <= 350e-6]
    if kind not in ("vref", "ss_charge", "enable", "ilim", "ovp") and len(steady) < 8:
        raise ProbeError("pwm_switching_state_missing", "fewer than eight observed steady pulses")
    if kind == "vref":
        value = _mean(t, w.y("V(vref)"), 250e-6, 350e-6)
        if abs(_mean(t, w.y("V(vdd)"), 250e-6, 350e-6) - 12) > 0.01:
            raise ProbeError("pwm_reference_bias_wrong")
    elif kind == "ss_charge":
        value = _mean(t, w.y("I(Vss)"), 250e-6, 350e-6)
        if np.max(np.abs(w.y("V(ss)"))) > 1e-5:
            raise ProbeError("pwm_ss_bias_wrong")
    elif kind == "frequency":
        rises = np.array([a for a, _ in steady])
        value = float(1 / np.mean(np.diff(rises)))
        rises_b = np.array([a for a, _ in pulses_b if 200e-6 <= a <= 350e-6])
        if len(rises_b) < 8 or abs((1 / np.mean(np.diff(rises_b))) / value - 1) > 0.01:
            raise ProbeError("pwm_output_frequencies_disagree")
    elif kind == "alternating":
        events = sorted(
            [(a, "A") for a, _ in pulses_a if 200e-6 <= a <= 350e-6]
            + [(a, "B") for a, _ in pulses_b if 200e-6 <= a <= 350e-6]
        )
        violations = sum(events[i][1] == events[i - 1][1] for i in range(1, len(events)))
        mask = (t >= 200e-6) & (t <= 350e-6)
        overlap = (ya[mask] > 6) & (yb[mask] > 6)
        sr_overlap = ((ya[mask] > 6) & (w.y("V(sra)")[mask] > 6)) | (
            (yb[mask] > 6) & (w.y("V(srb)")[mask] > 6)
        )
        value = float(violations + int(np.any(overlap)) + int(np.any(sr_overlap)))
    elif kind == "duty_response":
        early = _mean(t, (ya > 6).astype(float), 125e-6, 175e-6)
        late = _mean(t, (ya > 6).astype(float), 275e-6, 350e-6)
        comp_early = _mean(t, w.y("V(comp)"), 125e-6, 175e-6)
        comp_late = _mean(t, w.y("V(comp)"), 275e-6, 350e-6)
        if abs(comp_early - 0.7) > 0.05 or abs(comp_late - 1.6) > 0.05:
            raise ProbeError("pwm_comp_operating_points_wrong")
        value = float(late <= early + 0.03)
    elif kind in ("source_resistance", "sink_resistance"):
        mask = (t >= 200e-6) & (t <= 350e-6)
        if kind == "source_resistance":
            selection = ya[mask] > 6
            value = float(np.median((12 - ya[mask][selection]) / 0.02))
        else:
            selection = ya[mask] < 1
            value = float(np.median(ya[mask][selection] / 0.02))
        if np.count_nonzero(selection) < 20:
            raise ProbeError("pwm_driver_plateau_missing")
    elif kind in ("sp_delay", "ps_delay"):
        sr = w.y("V(sra)")
        if kind == "sp_delay":
            sr_falls = _edges(t, sr, False)
            delays = [
                a - sr_falls[np.searchsorted(sr_falls, a) - 1]
                for a, _ in steady
                if np.searchsorted(sr_falls, a) > 0
            ]
        else:
            sr_rises = _edges(t, sr)
            delays = [
                sr_rises[np.searchsorted(sr_rises, b)] - b
                for _, b in steady
                if np.searchsorted(sr_rises, b) < len(sr_rises)
            ]
        if len(delays) < 8 or not np.all(np.array(delays) > 0):
            raise ProbeError("pwm_dead_time_missing")
        value = float(np.median(delays))
    else:
        raise ProbeError("pwm_probe_unknown")
    if not np.isfinite(value):
        raise ProbeError("pwm_measurement_not_finite")
    return {"pwm_value": value}


def register(registry):
    from boardmodeler.authoring.probes import ProbeSpec

    for kind, unit in KINDS:
        name = "pwm_" + kind
        registry[name] = ProbeSpec(
            probe_id=name,
            title="PWM controller " + kind.replace("_", " "),
            question="What does the candidate do in the independently fixed controller fixture?",
            unit=unit,
            ports_needed=PORTS,
            judge_key="pwm_value",
            defaults={
                "temp_c": 25,
                "tstop_s": len(THRESHOLD_LEVELS[kind]) * UVLO_HOLD
                if kind in THRESHOLD_LEVELS
                else 400e-6,
                "tmax_s": 1e-9 if kind in {"sp_delay", "ps_delay", "ilim_delay"} else 20e-9,
                **(
                    {"delay_min_s": 12.3e-9, "delay_max_s": 38.7e-9} if kind == "ilim_delay" else {}
                ),
            },
            renderer=render,
            measurer=partial(measure, kind),
        )
