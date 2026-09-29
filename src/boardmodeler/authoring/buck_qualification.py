"""Reviewed M4b1 buck fixtures and state-aware measurements, shared by production and tools.

These functions preserve the original four-check nominal 25 C experiment. They do
not establish full family qualification, process corners, or SW ripple/edge accuracy.
No model parameter is read to set a test limit; verified source rows own the bounds.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from boardmodeler.authoring.buck_system_fixtures import (
    BuckBench,
    BuckBenchParts,
    CitedRow,
    _check_verified_row,
    render_deck,
)
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.simulation.raw import RawFile

ROW_IDS = {
    "vref": "B002_TPS54332DDA_VREF",
    "ss_charge": "B002_TPS54332DDA_SS_CHARGE",
    "shutdown_iq": "B002_TPS54332DDA_ISHDN",
    "operating_iq": "B002_TPS54332DDA_IOP_NONSW",
}
FAULT_OVERRIDES = {
    "vref": ("VREF", 0.72),
    "ss_charge": ("ISS", 1e-6),
    "shutdown_iq": ("EN_PULLUP", 10e-6),
    "operating_iq": ("IQOP", 200e-6),
}
SW_MAX_STEP_S = 50e-9
AVG_MAX_STEP_S = 1e-6
IQ_WINDOW_S = 0.5e-3


def _cited_rows(spec: SpecSet, requirements: Path) -> dict[str, CitedRow]:
    source = json.loads(requirements.read_text(encoding="utf-8"))
    if source.get("document", {}).get("doc_id") != spec.doc_id:
        raise ValueError("frozen requirements document differs from the spec")
    source_rows = {item["req_id"]: item for item in source["requirements"]}
    spec_rows = {item.char_id: item for item in spec.characteristics}
    result = {}
    for case, identifier in ROW_IDS.items():
        source_row = source_rows.get(identifier)
        characteristic = spec_rows.get(identifier)
        if (
            source_row is None
            or characteristic is None
            or source_row.get("citation_verified") is not True
            or characteristic.req_class == "ABSOLUTE_MAXIMUM"
        ):
            raise ValueError(f"{identifier}: missing verified operating citation")
        row = CitedRow.from_characteristic(characteristic)
        _check_verified_row(row, source_row, spec.doc_id)
        result[case] = row
    return result


def _replace_card(cards: list[str], name: str, replacement: str) -> None:
    indexes = [index for index, card in enumerate(cards) if card.split()[0] == name]
    if len(indexes) != 1:
        raise ValueError(f"M2 application needs exactly one {name} card")
    cards[indexes[0]] = replacement


def _case_bench(
    base: BuckBench, case: str, rows: dict[str, CitedRow], parts: BuckBenchParts
) -> BuckBench:
    cards = list(base.components)
    # Cin remains on the stimulus side. The shunt sees only DUT VIN current.
    cards.append("Vdutvin vin dut_vin 0")
    if case in {"shutdown_iq", "operating_iq"}:
        _replace_card(cards, "Vin", f"Vin vin 0 {parts.vin_v:.12g}")
    if case == "shutdown_iq":
        _replace_card(cards, "Ven", "Ven en 0 0")
    if case == "operating_iq":
        cards.append("Vvsense vsense 0 0.85")
    nodes = list(base.nodes)
    nodes[base.ports.index("VIN")] = "dut_vin"
    stop_s = 1e-3 if case == "shutdown_iq" else base.stop_s
    window_s = (stop_s - IQ_WINDOW_S, stop_s)
    source_rows = (rows[case],)
    if case in {"vref", "operating_iq"}:
        source_rows = (rows[case], rows["ss_charge"])
    if case == "ss_charge":
        source_rows = (rows[case], rows["vref"])
    return replace(
        base,
        name=case,
        nodes=tuple(nodes),
        components=tuple(cards),
        source_rows=source_rows,
        stop_s=stop_s,
        window_s=window_s,
        saved_signals=(),
        metadata={
            **base.metadata,
            "measurement_window_s": window_s,
            "ss_cap_f": parts.ss_cap_f,
            "fb_high_ohm": parts.fb_high_ohm,
            "fb_low_ohm": parts.fb_low_ohm,
            "vin_shunt": "Vdutvin vin dut_vin 0; Cin stays on vin",
            "vsense_forced_v": 0.85 if case == "operating_iq" else None,
        },
    )


def _deck(
    bench: BuckBench,
    model: Path,
    mode: str,
    variant: str,
    inductance_h: float,
) -> tuple[str, float]:
    max_step = SW_MAX_STEP_S if mode == "SW" else AVG_MAX_STEP_S
    signals = (
        "V(vin)",
        "V(dut_vin)",
        "V(en)",
        "V(ss)",
        "V(vsense)",
        "V(out)",
        "V(ph)",
        "V(comp)",
        "I(Lout)",
        "I(Vdutvin)",
    )
    checked = replace(bench, saved_signals=signals, max_step_s=max_step)
    # This verifier judges the saved waveform directly. The inherited M2
    # startup .meas thresholds are inapplicable to intentionally quiet IQ decks.
    lines = [
        line
        for line in render_deck(checked, model).splitlines()
        if not line.lstrip().lower().startswith((".meas ", ".measure "))
    ]
    instances = [index for index, line in enumerate(lines) if line.startswith("XU1 ")]
    if len(instances) != 1:
        raise ValueError("expected exactly one TPS54332 instance")
    overrides = []
    if mode == "AVG":
        overrides.append(f"L_EXT={inductance_h:.12g}")
    if variant == "fault":
        parameter, value = FAULT_OVERRIDES[bench.name]
        overrides.append(f"{parameter}={value:.12g}")
    if overrides:
        lines[instances[0]] += " " + " ".join(overrides)
    lines[0] = f"* Deterministic M4b1 {bench.name} {mode} {variant}; verdict requires raw/log"
    tran = [index for index, line in enumerate(lines) if line.startswith(".tran ")]
    if len(tran) != 1:
        raise ValueError("expected one transient command")
    lines.insert(tran[0], ".options plotwinsize=0")
    return "\n".join(lines) + "\n", max_step


def _traces(raw: RawFile, mode: str) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    axis = raw.time_column()
    if axis is None:
        raise ValueError("raw waveform has no time axis")
    t = np.asarray(axis, dtype=float)
    names = (
        "V(vin)",
        "V(dut_vin)",
        "V(en)",
        "V(ss)",
        "V(vsense)",
        "V(out)",
        "V(ph)",
        "V(comp)",
        "I(Lout)",
        "I(Vdutvin)",
    )
    signals = {name: np.asarray(raw.column(name), dtype=float) for name in names}
    if mode == "AVG" and raw.has("V(xU1.duty)"):
        signals["V(xU1.duty)"] = np.asarray(raw.column("V(xU1.duty)"), dtype=float)
    if len(t) < 3 or not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ValueError("raw waveform has nonfinite or decreasing time")
    if any(len(value) != len(t) or not np.all(np.isfinite(value)) for value in signals.values()):
        raise ValueError("raw waveform has missing or nonfinite saved signals")
    keep = np.r_[np.diff(t) > 0, True]
    t = t[keep]
    if len(t) < 3:
        raise ValueError("raw waveform has fewer than three distinct times")
    return t, {name: value[keep] for name, value in signals.items()}


def _window(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> np.ndarray:
    if start < t[0] or end > t[-1] or start >= end:
        raise ValueError(f"waveform does not cover {start:g}..{end:g} s")
    inside = (t > start) & (t < end)
    times = np.r_[start, t[inside], end]
    if len(times) < 3:
        raise ValueError(f"waveform window {start:g}..{end:g} has no interior sample")
    return np.column_stack((times, np.interp(times, t, signal)))


def _stats(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> dict[str, float]:
    window = _window(t, signal, start, end)
    times, values = window[:, 0], window[:, 1]
    midpoint = (start + end) / 2
    early = _window(t, signal, start, midpoint)
    late = _window(t, signal, midpoint, end)
    return {
        "mean": float(np.trapezoid(values, times) / (end - start)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "early_mean": float(np.trapezoid(early[:, 1], early[:, 0]) / (midpoint - start)),
        "late_mean": float(np.trapezoid(late[:, 1], late[:, 0]) / (end - midpoint)),
    }


def _up_crossing(t: np.ndarray, signal: np.ndarray, level: float) -> float | None:
    indexes = np.flatnonzero((signal[:-1] < level) & (signal[1:] >= level))
    if not len(indexes):
        return None
    index = int(indexes[0])
    fraction = (level - signal[index]) / (signal[index + 1] - signal[index])
    return float(t[index] + fraction * (t[index + 1] - t[index]))


def _state_iq(
    case: str, mode: str, t: np.ndarray, signals: dict[str, np.ndarray], window: tuple[float, float]
) -> dict[str, Any]:
    start, end = window
    measured = {
        name: _stats(t, signals[name], start, end)
        for name in ("V(dut_vin)", "V(en)", "V(ss)", "V(vsense)", "V(out)", "V(ph)", "I(Lout)")
    }
    ph = _window(t, signals["V(ph)"], start, end)[:, 1]
    high_edges = int(np.count_nonzero((ph[:-1] < 1.0) & (ph[1:] >= 1.0)))
    checks = {
        "vin_12v": measured["V(dut_vin)"]["min"] >= 11.9 and measured["V(dut_vin)"]["max"] <= 12.1,
        "ph_quiet": high_edges == 0 and measured["V(ph)"]["max"] < 1.0,
        "inductor_quiet": max(abs(measured["I(Lout)"]["min"]), abs(measured["I(Lout)"]["max"]))
        < 0.01,
    }
    if case == "shutdown_iq":
        checks.update(
            en_low=measured["V(en)"]["max"] < 0.1,
            ss_discharged=measured["V(ss)"]["max"] < 0.1,
            out_low=measured["V(out)"]["max"] < 0.1,
        )
    else:
        checks.update(
            en_high=measured["V(en)"]["min"] > 2.0,
            ss_complete=measured["V(ss)"]["min"] > 0.82,
            vsense_forced=measured["V(vsense)"]["min"] >= 0.845
            and measured["V(vsense)"]["max"] <= 0.855,
        )
    if mode == "AVG" and "V(xU1.duty)" in signals:
        duty = _stats(t, signals["V(xU1.duty)"], start, end)
        measured["V(xU1.duty)"] = duty
    return {
        "status": "READY" if all(checks.values()) else "UNKNOWN",
        "checks": checks,
        "measurements": measured,
        "ph_rising_edges_above_1v": high_edges,
        "reason": None if all(checks.values()) else "requested non-switching state not observed",
    }


def _stable_iq_draw(draw: dict[str, float]) -> bool:
    mean = abs(draw["mean"])
    drift = abs(draw["late_mean"] - draw["early_mean"])
    span = draw["max"] - draw["min"]
    return drift <= max(0.05 * mean, 0.2e-6) and span <= max(0.1 * mean, 0.2e-6)


def _measure(
    case: str,
    mode: str,
    bench: BuckBench,
    row: CitedRow,
    raw: RawFile,
) -> dict[str, Any]:
    t, signals = _traces(raw, mode)
    start, end = bench.window_s
    common = {
        "raw_points": raw.npoints,
        "distinct_time_points": len(t),
        "window_s": [start, end],
    }
    if case == "vref":
        sense = _stats(t, signals["V(vsense)"], start, end)
        out = _stats(t, signals["V(out)"], start, end)
        ss = _stats(t, signals["V(ss)"], start, end)
        vin = _stats(t, signals["V(dut_vin)"], start, end)
        current = _stats(t, signals["I(Lout)"], start, end)
        divider = float(bench.metadata["fb_low_ohm"]) / (
            float(bench.metadata["fb_high_ohm"]) + float(bench.metadata["fb_low_ohm"])
        )
        checks = {
            "vin_12v": vin["min"] >= 11.9 and vin["max"] <= 12.1,
            "ss_above_reference": ss["min"] > (row.max_value or row.typ_value or 0),
            "positive_delivered_current": current["mean"] > 0.1,
            "positive_output": out["mean"] > 0.5,
            "feedback_divider_consistent": abs(sense["mean"] - divider * out["mean"]) <= 0.01,
            "sense_settled": sense["max"] - sense["min"] <= 0.02 * abs(sense["mean"]),
            "output_settled": out["max"] - out["min"] <= 0.02 * abs(out["mean"]),
        }
        common.update(
            value=sense["mean"],
            unit="V",
            state={"status": "READY" if all(checks.values()) else "UNKNOWN", "checks": checks},
            metrics={
                "vsense_v": sense,
                "vout_v": out,
                "ss_v": ss,
                "vin_dut_v": vin,
                "inductor_a": current,
                "feedback_divider_fraction": divider,
            },
        )
        return common
    if case == "ss_charge":
        ss = signals["V(ss)"]
        t35, t45 = _up_crossing(t, ss, 0.35), _up_crossing(t, ss, 0.45)
        if t35 is None or t45 is None or t45 <= t35:
            raise ValueError("SS waveform has no ordered 0.35/0.45 V crossings")
        vin_mid = float(np.interp((t35 + t45) / 2, t, signals["V(dut_vin)"]))
        en_mid = float(np.interp((t35 + t45) / 2, t, signals["V(en)"]))
        current_a = float(bench.metadata["ss_cap_f"]) * 0.1 / (t45 - t35)
        checks = {
            "vin_12v": 11.9 <= vin_mid <= 12.1,
            "en_high": en_mid > 2.0,
            "window_near_cited_ss_point": t35 >= 0 and t45 <= bench.stop_s,
        }
        common.update(
            value=current_a,
            unit="A",
            state={"status": "READY" if all(checks.values()) else "UNKNOWN", "checks": checks},
            metrics={
                "ss_voltage_interval_v": [0.35, 0.45],
                "ss_crossing_times_s": [t35, t45],
                "ss_cap_f": bench.metadata["ss_cap_f"],
                "vin_at_mid_v": vin_mid,
                "en_at_mid_v": en_mid,
                "method": "external Css * 0.1 V / crossing time near cited Vss=0.4 V",
            },
        )
        return common
    state = _state_iq(case, mode, t, signals, bench.window_s)
    draw = _stats(t, signals["I(Vdutvin)"], start, end)
    spread = abs(draw["late_mean"] - draw["early_mean"])
    stable = _stable_iq_draw(draw)
    state["checks"]["input_draw_stable"] = stable
    state["checks"]["input_draw_nonnegative"] = draw["mean"] >= 0
    state["status"] = "READY" if all(state["checks"].values()) else "UNKNOWN"
    if state["status"] == "UNKNOWN":
        state["reason"] = "requested stable non-switching VIN-current state not observed"
    common.update(
        value=draw["mean"],
        unit="A",
        state=state,
        metrics={
            "dut_vin_current_a": draw,
            "input_draw_half_window_delta_a": spread,
            "source_current_sign": "positive I(Vdutvin) flows from source-side Cin into DUT VIN",
            "input_cap_excluded": True,
        },
    )
    return common


def _judge(row: CitedRow, measurement: dict[str, Any]) -> dict[str, Any]:
    value = float(measurement["value"])
    if not math.isfinite(value):
        return {"verdict": "UNKNOWN", "reason": "nonfinite waveform value"}
    if measurement["state"]["status"] != "READY":
        return {
            "verdict": "UNKNOWN",
            "reason": measurement["state"].get("reason", "state not ready"),
        }
    typical = None
    if row.typ_value is not None:
        typical = {
            "value": row.typ_value,
            "band": [0.9 * row.typ_value, 1.1 * row.typ_value],
            "status": "WITHIN"
            if 0.9 * row.typ_value <= value <= 1.1 * row.typ_value
            else "OUTSIDE",
            "scope": "nominal 25 C typical comparison; not a process-corner claim",
        }
    if row.min_value is not None or row.max_value is not None:
        lower = row.min_value if row.min_value is not None else 0.0
        upper = row.max_value if row.max_value is not None else math.inf
        return {
            "verdict": "PASS" if lower <= value <= upper else "FAIL",
            "basis": "cited numeric min/max at this fixture condition",
            "band": [lower, upper if math.isfinite(upper) else None],
            "typical_comparison": typical,
            "reason": None,
        }
    if typical is None:
        return {"verdict": "UNKNOWN", "reason": "cited row has no numeric limit"}
    return {
        "verdict": "PASS" if typical["status"] == "WITHIN" else "FAIL",
        "basis": "cited typical +/-10% at this fixture condition",
        "band": typical["band"],
        "typical_comparison": typical,
        "reason": None,
    }
