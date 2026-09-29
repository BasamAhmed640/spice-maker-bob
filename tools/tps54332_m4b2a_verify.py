r"""Measure both directions of TPS54332 VIN UVLO and EN thresholds.

The model is rendered afresh from frozen cited inputs. Each LTspice deck uses
the M2 external application, with a zero-volt DUT-VIN shunt after Cin. Stage
activity is observed through that external shunt and output-inductor current;
no parameter declaration or internal logic node can establish a verdict.

PowerShell example::

    .\.venv\Scripts\python.exe tools\tps54332_m4b2a_verify.py --ltspice-exe "C:\path\to\LTspice.exe" --requirements models\T1-tps54332\spec\requirements.json --bindings models\T1-tps54332\spec\bindings.json --out runs\m4b2a
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

os.environ["BOARDMODELER_NO_NETWORK"] = "1"
os.environ["HARNESS_WORKERS"] = "1"

from boardmodeler.authoring.buck_system_fixtures import (
    BuckBench,
    BuckBenchParts,
    CitedRow,
    _check_verified_row,
    build_buck_system_benches,
    render_deck,
)
from boardmodeler.authoring.spec import SpecSet, load_tps54320_spec
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import RawFile, read_raw

REPO = Path(__file__).resolve().parents[1]
PART = "TPS54332DDA"
MODES = ("SW", "AVG")
CASES = ("uvlo", "en")
VARIANTS = ("clean", "fault")
DIRECTIONS = ("rising", "falling")
FROZEN_REQUIREMENTS = REPO / "models/T1-tps54332/spec/requirements.json"
FROZEN_BINDINGS = REPO / "models/T1-tps54332/spec/bindings.json"
REQUIREMENTS_SHA256 = "3884fd76976e071cd2ea81d5db64cb1bde17aecbaccc8f87399e7448646cc060"
BINDINGS_SHA256 = "1b3d45e8977948ce2ed7f1079db2aaa598b543cd65a83b697af8455d6345ad57"
SPEC_DIGEST = "499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7"
SW_LIBRARY_SHA256 = "df4abecd09c5642360499450ae8123b51ef8e75beccc9df03832fec189da948f"
AVG_LIBRARY_SHA256 = "718b763aa006de166b6807536861026a49892525047c163ae30fcdaf5bdb525c"
ROW_IDS = {
    "uvlo": "B002_TPS54332DDA_UVLO_VIN",
    "en": "B002_TPS54332DDA_EN_TH",
}
FAULT_OVERRIDES = {"uvlo": ("UVTH", 2.5), "en": ("ENTH", 1.0)}
RAMP_UP_S = (0.0, 2e-3)
ACTIVE_S = (7.5e-3, 8e-3)
RAMP_DOWN_S = (8e-3, 10e-3)
PRE_QUIET_S = (0.1e-3, 0.3e-3)
POST_QUIET_S = (10.2e-3, 10.5e-3)
STOP_S = 10.6e-3
SW_MAX_STEP_S = 50e-9
AVG_MAX_STEP_S = 1e-6
STAGE_CURRENT_A = 0.05  # Synthetic external-stage activity discriminator.
QUIET_CURRENT_A = 0.01  # Synthetic fixture guard, not a datasheet IQ limit.
SS_ONSET_V = 10e-6  # Synthetic SS-pin event discriminator, not a cited threshold.
SS_RISE_PERSISTENCE_S = 30e-6
SS_RISE_MIN_GAIN_V = 2e-3
SS_FALL_SLOPE_V_PER_S = -10_000.0
LOCAL_STAGE_WINDOW_S = 50e-6
LOCAL_INDUCTOR_A = 0.05
LOCAL_OUTPUT_GAIN_V = 0.02
LOCAL_AVG_PH_V = 0.02


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ltspice-exe", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--emit-only", action="store_true")
    return parser.parse_args()


def _validate(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    exe = args.ltspice_exe.resolve(strict=True)
    requirements = args.requirements.resolve(strict=True)
    bindings = args.bindings.resolve(strict=True)
    out = args.out.resolve()
    if not exe.is_file() or not requirements.is_file() or not bindings.is_file():
        raise ValueError("LTspice, requirements, and bindings must be explicit files")
    if requirements != FROZEN_REQUIREMENTS.resolve() or bindings != FROZEN_BINDINGS.resolve():
        raise ValueError("M4b2a requires the frozen local TPS54332 inputs")
    if _sha256(requirements) != REQUIREMENTS_SHA256 or _sha256(bindings) != BINDINGS_SHA256:
        raise ValueError("frozen TPS54332 requirements or bindings changed")
    if not out.is_relative_to((REPO / "runs").resolve()):
        raise ValueError("--out must be inside this repository's ignored runs/ directory")
    if len(str(out)) > 110:
        raise ValueError("Choose a shorter --out path for LTspice on Windows")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("--out must be an empty directory, to avoid mixing evidence")
    if not math.isfinite(args.timeout_s) or args.timeout_s <= 0:
        raise ValueError("--timeout-s must be a positive finite number")
    return exe, requirements, bindings, out


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
        if row.unit != "V" or row.min_value is None:
            raise ValueError(f"{identifier}: cited voltage threshold has no minimum")
        result[case] = row
    if (result["uvlo"].min_value, result["uvlo"].max_value) != (3.5, None):
        raise ValueError("frozen UVLO rising/falling bound changed")
    if (result["en"].min_value, result["en"].max_value) != (1.25, 1.35):
        raise ValueError("frozen EN rising/falling band changed")
    return result


def _replace_card(cards: list[str], name: str, replacement: str) -> None:
    indexes = [index for index, card in enumerate(cards) if card.split()[0] == name]
    if len(indexes) != 1:
        raise ValueError(f"M2 application needs exactly one {name} card")
    cards[indexes[0]] = replacement


def _bench(base: BuckBench, case: str, row: CitedRow, parts: BuckBenchParts) -> BuckBench:
    cards = list(base.components)
    # Cin remains on the stimulus side; I(Vdutvin) excludes its charging current.
    cards.append("Vdutvin vin dut_vin 0")
    if case == "uvlo":
        _replace_card(
            cards,
            "Vin",
            "Vin vin 0 PWL(0 0 2m 6 8m 6 10m 1.5 10.5m 1.5)",
        )
    elif case == "en":
        _replace_card(cards, "Vin", f"Vin vin 0 {parts.vin_v:.12g}")
        _replace_card(
            cards,
            "Ven",
            "Ven en 0 PWL(0 0 2m 3.3 8m 3.3 10m 0 10.5m 0)",
        )
    else:
        raise ValueError(f"unsupported threshold case {case}")
    nodes = list(base.nodes)
    nodes[base.ports.index("VIN")] = "dut_vin"
    return replace(
        base,
        name=f"{case}_bidirectional",
        nodes=tuple(nodes),
        components=tuple(cards),
        source_rows=(row,),
        stop_s=STOP_S,
        window_s=(0.0, STOP_S),
        measures=(),
        saved_signals=(),
        metadata={
            **base.metadata,
            "vin_shunt": "Vdutvin vin dut_vin 0; Cin stays on vin",
            "rise_ramp_s": RAMP_UP_S,
            "active_window_s": ACTIVE_S,
            "fall_ramp_s": RAMP_DOWN_S,
            "pre_quiet_window_s": PRE_QUIET_S,
            "post_quiet_window_s": POST_QUIET_S,
            "stage_current_threshold_a": STAGE_CURRENT_A,
            "quiet_current_guard_a": QUIET_CURRENT_A,
            "ss_onset_guard_v": SS_ONSET_V,
            "ss_rise_persistence_s": SS_RISE_PERSISTENCE_S,
            "ss_rise_min_gain_v": SS_RISE_MIN_GAIN_V,
            "ss_fall_slope_v_per_s": SS_FALL_SLOPE_V_PER_S,
            "local_stage_window_s": LOCAL_STAGE_WINDOW_S,
            "local_inductor_guard_a": LOCAL_INDUCTOR_A,
            "local_output_gain_guard_v": LOCAL_OUTPUT_GAIN_V,
            "local_avg_ph_guard_v": LOCAL_AVG_PH_V,
            "threshold_stimulus": "V(dut_vin)" if case == "uvlo" else "V(en)",
        },
    )


def _deck(
    bench: BuckBench, model: Path, mode: str, case: str, variant: str, inductance_h: float
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
        "I(Lout)",
        "I(Vdutvin)",
    )
    checked = replace(bench, saved_signals=signals, max_step_s=max_step)
    lines = render_deck(checked, model).splitlines()
    instances = [index for index, line in enumerate(lines) if line.startswith("XU1 ")]
    if len(instances) != 1:
        raise ValueError("expected exactly one TPS54332 instance")
    overrides = []
    if mode == "AVG":
        overrides.append(f"L_EXT={inductance_h:.12g}")
    if variant == "fault":
        parameter, value = FAULT_OVERRIDES[case]
        overrides.append(f"{parameter}={value:.12g}")
    if overrides:
        lines[instances[0]] += " " + " ".join(overrides)
    lines[0] = f"* Deterministic M4b2a {case} {mode} {variant}; verdict needs raw/log"
    tran = [index for index, line in enumerate(lines) if line.startswith(".tran ")]
    if len(tran) != 1:
        raise ValueError("expected one transient command")
    lines.insert(tran[0], ".options plotwinsize=0")
    return "\n".join(lines) + "\n", max_step


def _traces(raw: RawFile) -> tuple[np.ndarray, dict[str, np.ndarray]]:
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
        "I(Lout)",
        "I(Vdutvin)",
    )
    signals = {name: np.asarray(raw.column(name), dtype=float) for name in names}
    if len(t) < 3 or not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ValueError("raw waveform has nonfinite or decreasing time")
    if any(len(value) != len(t) or not np.all(np.isfinite(value)) for value in signals.values()):
        raise ValueError("raw waveform has missing or nonfinite saved signals")
    # LTspice sometimes writes two states at the same time; retain the last.
    keep = np.r_[np.diff(t) > 0, True]
    t = t[keep]
    signals = {name: value[keep] for name, value in signals.items()}
    if len(t) < 3 or t[0] > 0 or t[-1] < STOP_S - 1e-8:
        raise ValueError("raw waveform does not cover the declared ramp and state windows")
    return t, signals


def _window(
    t: np.ndarray, signal: np.ndarray, start: float, end: float
) -> tuple[np.ndarray, np.ndarray]:
    if not 0 <= start < end <= t[-1] or start < t[0]:
        raise ValueError(f"waveform does not cover window {start:g}..{end:g} s")
    inside = (t > start) & (t < end)
    times = np.r_[start, t[inside], end]
    values = np.r_[np.interp(start, t, signal), signal[inside], np.interp(end, t, signal)]
    return times, values


def _stats(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> dict[str, float]:
    times, values = _window(t, signal, start, end)
    return {
        "mean": float(np.trapezoid(values, times) / (end - start)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "p95": float(np.percentile(values, 95)),
        "p99_abs": float(np.percentile(np.abs(values), 99)),
    }


def _stage_state(mode: str, t: np.ndarray, signals: dict[str, np.ndarray]) -> dict[str, Any]:
    current = signals["I(Vdutvin)"]
    inductor = signals["I(Lout)"]
    output = signals["V(out)"]
    ph = signals["V(ph)"]
    pre = _stats(t, current, *PRE_QUIET_S)
    active_current = _stats(t, current, *ACTIVE_S)
    active_inductor = _stats(t, inductor, *ACTIVE_S)
    active_output = _stats(t, output, *ACTIVE_S)
    active_ph = _stats(t, ph, *ACTIVE_S)
    post = _stats(t, current, *POST_QUIET_S)
    active_ph_time, active_ph_values = _window(t, ph, *ACTIVE_S)
    ph_edges = int(np.count_nonzero((active_ph_values[:-1] < 1.0) & (active_ph_values[1:] >= 1.0)))
    checks = {
        "pre_stage_quiet": pre["p99_abs"] < QUIET_CURRENT_A,
        "active_dut_input_draw": active_current["p95"] > STAGE_CURRENT_A,
        "active_output_inductor": active_inductor["mean"] > 0.2,
        "active_output_voltage": active_output["mean"] > 1.5,
        "post_stage_quiet": post["p99_abs"] < QUIET_CURRENT_A,
        "sw_ph_edges" if mode == "SW" else "avg_ph_delivery": (
            ph_edges > 2 if mode == "SW" else active_ph["mean"] > 0.5
        ),
    }
    return {
        "status": "READY" if all(checks.values()) else "UNKNOWN",
        "checks": checks,
        "windows_s": {
            "pre_quiet": PRE_QUIET_S,
            "active": ACTIVE_S,
            "post_quiet": POST_QUIET_S,
        },
        "metrics": {
            "dut_input_current_a": {"pre": pre, "active": active_current, "post": post},
            "active_inductor_a": active_inductor,
            "active_output_v": active_output,
            "active_ph_v": active_ph,
            "sw_ph_rising_edges_above_1v": ph_edges if mode == "SW" else None,
            "active_ph_window_points": len(active_ph_time),
        },
        "reason": None if all(checks.values()) else "external power-stage states not observed",
    }


def _event(
    t: np.ndarray,
    stimulus: np.ndarray,
    signals: dict[str, np.ndarray],
    mode: str,
    direction: str,
) -> dict[str, Any]:
    ramp = RAMP_UP_S if direction == "rising" else RAMP_DOWN_S
    start, end = ramp
    ss = signals["V(ss)"]
    current = signals["I(Vdutvin)"]
    if direction == "rising":
        indexes = (
            np.flatnonzero(
                (t[1:] >= start) & (t[1:] <= end) & (ss[:-1] < SS_ONSET_V) & (ss[1:] >= SS_ONSET_V)
            )
            + 1
        )
    else:
        slope = np.diff(ss) / np.diff(t)
        indexes = (
            np.flatnonzero((t[1:] >= start) & (t[1:] <= end) & (slope <= SS_FALL_SLOPE_V_PER_S)) + 1
        )
    if not len(indexes):
        return {
            "status": "UNKNOWN",
            "reason": f"no external SS-pin {direction} transition on ramp",
            "ramp_s": ramp,
            "value": None,
        }
    # An early isolated SS glitch is ambiguous: never skip it to award a later PASS.
    index = int(indexes[0])
    event_t = float(t[index])
    event_v = float(stimulus[index])
    margin = 0.1e-3
    if not start + margin < event_t < end - margin:
        return {
            "status": "UNKNOWN",
            "reason": f"{direction} SS-pin transition touches ramp boundary",
            "ramp_s": ramp,
            "event_time_s": event_t,
            "value": event_v,
        }
    if direction == "rising":
        quiet = _stats(t, ss, event_t - 50e-6, event_t - 2e-6)
        ss_window_time, ss_window = _window(t, ss, event_t, event_t + SS_RISE_PERSISTENCE_S)
        ss_gain = float(ss_window[-1] - ss_window[0])
        local_end = event_t + LOCAL_STAGE_WINDOW_S
        local_il = _stats(t, signals["I(Lout)"], event_t, local_end)
        local_ph = _stats(t, signals["V(ph)"], event_t, local_end)
        _, ph_values = _window(t, signals["V(ph)"], event_t, local_end)
        _, out_values = _window(t, signals["V(out)"], event_t, local_end)
        ph_edges = int(np.count_nonzero((ph_values[:-1] < 1.0) & (ph_values[1:] >= 1.0)))
        out_gain = float(out_values[-1] - out_values[0])
        checks = {
            "before_ss_quiet": quiet["p99_abs"] < SS_ONSET_V,
            "ss_charge_persists": ss_gain >= SS_RISE_MIN_GAIN_V
            and float(np.min(ss_window)) >= SS_ONSET_V / 2,
            "local_inductor_delivery": local_il["mean"] > LOCAL_INDUCTOR_A,
            "local_output_growth": out_gain > LOCAL_OUTPUT_GAIN_V,
            "local_sw_ph_edges" if mode == "SW" else "local_avg_ph_delivery": (
                ph_edges >= 2 if mode == "SW" else local_ph["mean"] > LOCAL_AVG_PH_V
            ),
        }
        evidence = {
            "before_ss_v": quiet,
            "ss_gain_30us_v": ss_gain,
            "ss_window_points": len(ss_window_time),
            "local_inductor_a": local_il,
            "local_ph_v": local_ph,
            "local_ph_edges": ph_edges if mode == "SW" else None,
            "local_output_gain_v": out_gain,
        }
    else:
        active = _stats(t, current, event_t - 0.1e-3, event_t)
        quiet = _stats(t, current, event_t + 0.02e-3, event_t + 0.1e-3)
        ss_before = float(np.interp(event_t - 5e-6, t, ss))
        ss_after_10us = float(np.interp(event_t + 10e-6, t, ss))
        ss_after_20us = float(np.interp(event_t + 20e-6, t, ss))
        checks = {
            "before_event_active": active["p95"] > STAGE_CURRENT_A,
            "after_event_quiet": quiet["p99_abs"] < QUIET_CURRENT_A,
            "ss_charged_before": ss_before > 0.5,
            "ss_discharge_persists": ss_before - ss_after_10us > 0.5 and ss_after_20us < 0.01,
        }
        evidence = {
            "near_current_a": {"active": active, "quiet": quiet},
            "ss_before_v": ss_before,
            "ss_after_10us_v": ss_after_10us,
            "ss_after_20us_v": ss_after_20us,
            "ss_fall_slope_v_per_s": float(slope[index - 1]),
        }
    return {
        "status": "READY" if all(checks.values()) else "UNKNOWN",
        "reason": None if all(checks.values()) else "external SS-pin/stage transition ambiguous",
        "ramp_s": ramp,
        "event_time_s": event_t,
        "value": event_v,
        "unit": "V",
        "checks": checks,
        "evidence": evidence,
        "method": "first persistent external SS-pin transition with external power-stage corroboration",
    }


def _measure(case: str, mode: str, raw: RawFile) -> dict[str, Any]:
    t, signals = _traces(raw)
    stage = _stage_state(mode, t, signals)
    stimulus_name = "V(dut_vin)" if case == "uvlo" else "V(en)"
    stimulus = signals[stimulus_name]
    ramp_checks = {}
    for direction, (start, end) in zip(DIRECTIONS, (RAMP_UP_S, RAMP_DOWN_S), strict=True):
        times, values = _window(t, stimulus, start, end)
        differences = np.diff(values)
        ramp_checks[direction] = {
            "monotonic": bool(
                np.all(differences >= -1e-4)
                if direction == "rising"
                else np.all(differences <= 1e-4)
            ),
            "stimulus_span_v": [float(values[0]), float(values[-1])],
            "time_points": len(times),
        }
    directions = {}
    for direction in DIRECTIONS:
        event = _event(t, stimulus, signals, mode, direction)
        event["stage_status"] = stage["status"]
        event["stimulus_ramp"] = ramp_checks[direction]
        if stage["status"] != "READY" or not ramp_checks[direction]["monotonic"]:
            event["status"] = "UNKNOWN"
            event["reason"] = (
                stage["reason"]
                if stage["status"] != "READY"
                else f"{direction} stimulus ramp not monotonic"
            )
        directions[direction] = event
    return {
        "raw_points": raw.npoints,
        "distinct_time_points": len(t),
        "stimulus": stimulus_name,
        "stage": stage,
        "directions": directions,
        "cited_temperature_claim": "nominal .temp 25 only; not a process or temperature-corner claim",
    }


def _judge(row: CitedRow, event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("value")
    if event.get("status") != "READY" or value is None or not math.isfinite(float(value)):
        return {"verdict": "UNKNOWN", "reason": event.get("reason") or "stage event unavailable"}
    lower = row.min_value
    upper = row.max_value
    if lower is None:
        return {"verdict": "UNKNOWN", "reason": "cited threshold has no lower bound"}
    within = value >= lower and (upper is None or value <= upper)
    return {
        "verdict": "PASS" if within else "FAIL",
        "reason": None,
        "basis": "observed external SS-pin transition and power-stage evidence compared with frozen cited row",
        "band_v": [lower, upper],
        "observed_v": value,
    }


def _overall_verdict(directions: dict[str, Any]) -> str:
    verdicts = {item["judgement"]["verdict"] for item in directions.values()}
    if verdicts == {"PASS"}:
        return "PASS"
    if "FAIL" in verdicts:
        return "FAIL"
    return "UNKNOWN"


def _run_one(
    *,
    case: str,
    mode: str,
    variant: str,
    bench: BuckBench,
    row: CitedRow,
    model: Path,
    exe: Path,
    out: Path,
    inductance_h: float,
    timeout_s: float,
    keep_raw: bool,
    emit_only: bool,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    folder = out / f"{case}-{mode.lower()}-{variant}"
    folder.mkdir(parents=True, exist_ok=True)
    deck = folder / "deck.cir"
    deck_text, max_step = _deck(bench, model, mode, case, variant, inductance_h)
    deck.write_text(deck_text, encoding="utf-8", newline="\n")
    record: dict[str, Any] = {
        "schema_version": 1,
        "case": case,
        "mode": mode,
        "variant": variant,
        "recorded_utc": datetime.now(UTC).isoformat(),
        "status": "DECK_ONLY" if emit_only else "RUN_FAILED",
        "verdict": "UNJUDGED",
        "deck": str(deck),
        "deck_sha256": _sha256(deck),
        "model_sha256": provenance["model_sha256"][mode],
        "requirements_sha256": provenance["requirements_sha256"],
        "bindings_sha256": provenance["bindings_sha256"],
        "ltspice_exe": str(exe),
        "ltspice_exe_sha256": provenance["ltspice_exe_sha256"],
        "script_sha256": provenance["script_sha256"],
        "source_row": asdict(row),
        "bench_components": list(bench.components),
        "bench_metadata": bench.metadata,
        "max_step_s": max_step,
        "l_ext_h": inductance_h if mode == "AVG" else None,
        "fault_override": dict([FAULT_OVERRIDES[case]]) if variant == "fault" else None,
    }
    if emit_only:
        record["reason"] = "No LTspice waveform was requested"
        _write_json(folder / "result.json", record)
        return record
    run = None
    started = time.perf_counter()
    try:
        run = run_batch(exe, deck, folder, timeout_s=timeout_s, lock_timeout_s=30.0)
        record.update(
            ltspice_wall_s=round(run.wall_s, 3),
            elapsed_s=round(time.perf_counter() - started, 3),
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            simulator_observed=run.observed(),
            log_sha256=_sha256(run.log_path) if run.log_path else None,
            raw_sha256=_sha256(run.raw_path) if run.raw_path else None,
            raw_bytes=run.raw_path.stat().st_size if run.raw_path else None,
            op_raw_sha256=_sha256(run.op_raw_path) if run.op_raw_path else None,
            op_raw_bytes=run.op_raw_path.stat().st_size if run.op_raw_path else None,
        )
        if run.ok and run.raw_path and run.log_path:
            try:
                measurement = _measure(case, mode, read_raw(run.raw_path))
                directions = {
                    direction: {
                        "event": measurement["directions"][direction],
                        "judgement": _judge(row, measurement["directions"][direction]),
                    }
                    for direction in DIRECTIONS
                }
                record.update(
                    status="MEASURED",
                    measurement=measurement,
                    directions=directions,
                    verdict=_overall_verdict(directions),
                )
                if record["verdict"] == "UNKNOWN":
                    record["reason"] = "one or both external stage transitions unresolved"
            except (KeyError, TypeError, ValueError) as exc:
                record.update(status="UNKNOWN", verdict="UNKNOWN")
                record["reason"] = f"waveform measurement unavailable: {type(exc).__name__}: {exc}"
        else:
            record["reason"] = "LTspice did not finish with a readable raw/log waveform"
    except Exception as exc:
        record["elapsed_s"] = round(time.perf_counter() - started, 3)
        record["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if run is not None and not keep_raw:
            for artifact in (run.raw_path, run.op_raw_path):
                if artifact is not None:
                    artifact.unlink(missing_ok=True)
        record["raw_retained"] = bool(run is not None and run.raw_path and keep_raw)
        _write_json(folder / "result.json", record)
    return record


def main() -> int:
    args = _args()
    exe, requirements, bindings, out = _validate(args)
    spec = load_tps54320_spec(requirements, bindings, part=PART, subckt=PART)
    if spec.digest() != SPEC_DIGEST:
        raise ValueError("frozen TPS54332 spec digest changed")
    sw = seed_from_spec(spec, mode="SW")
    avg = seed_from_spec(spec, mode="AVG")
    if sw is None or avg is None or sw.part != PART or avg.part != PART:
        raise ValueError("frozen TPS54332 spec did not render both modes")
    if sw.ports != avg.ports or sw.parameters != avg.parameters:
        raise ValueError("SW/AVG physical ports or cited device-parameter origins differ")
    rows = _cited_rows(spec, requirements)
    parts = BuckBenchParts()
    m2 = build_buck_system_benches(spec, parts, requirements_path=requirements)
    startup = next((bench for bench in m2 if bench.name == "startup"), None)
    if startup is None or startup.ports != sw.ports:
        raise ValueError("M2 startup application or physical-port contract changed")
    benches = {case: _bench(startup, case, rows[case], parts) for case in CASES}
    out.mkdir(parents=True, exist_ok=True)
    models = {"SW": out / "sw-model.lib", "AVG": out / "avg-model.lib"}
    sw.write(models["SW"])
    avg.write(models["AVG"])
    hashes = {mode: _sha256(model) for mode, model in models.items()}
    if hashes != {"SW": SW_LIBRARY_SHA256, "AVG": AVG_LIBRARY_SHA256}:
        raise ValueError("frozen SW or AVG rendered model hash changed")
    if ".param L_EXT=" not in avg.library_text:
        raise ValueError("AVG model lacks instance L_EXT")
    provenance = {
        "schema_version": 1,
        "part": PART,
        "spec_digest": spec.digest(),
        "requirements_sha256": _sha256(requirements),
        "bindings_sha256": _sha256(bindings),
        "ltspice_exe_sha256": _sha256(exe),
        "script_sha256": _sha256(Path(__file__)),
        "model_sha256": hashes,
        "contract_sha256": sw.contract_sha256,
        "physical_ports": list(sw.ports),
        "parameters": [parameter.payload() for parameter in sw.parameters],
        "passive_source": parts.source,
        "external_inductance_h": parts.inductance_h,
        "temperature_c": 25,
        "verdict_scope": "cited UVLO/EN nominal waveform thresholds in both directions; not process corners",
        "stage_activity_guard_a": STAGE_CURRENT_A,
        "stage_quiet_guard_a": QUIET_CURRENT_A,
        "ss_onset_guard_v": SS_ONSET_V,
        "ss_rise_persistence_s": SS_RISE_PERSISTENCE_S,
        "ss_rise_min_gain_v": SS_RISE_MIN_GAIN_V,
        "ss_fall_slope_v_per_s": SS_FALL_SLOPE_V_PER_S,
        "local_stage_window_s": LOCAL_STAGE_WINDOW_S,
        "local_inductor_guard_a": LOCAL_INDUCTOR_A,
        "local_output_gain_guard_v": LOCAL_OUTPUT_GAIN_V,
        "local_avg_ph_guard_v": LOCAL_AVG_PH_V,
    }
    _write_json(out / "provenance.json", provenance)
    records = []
    for case in CASES:
        for mode in MODES:
            for variant in VARIANTS:
                record = _run_one(
                    case=case,
                    mode=mode,
                    variant=variant,
                    bench=benches[case],
                    row=rows[case],
                    model=models[mode],
                    exe=exe,
                    out=out,
                    inductance_h=parts.inductance_h,
                    timeout_s=args.timeout_s,
                    keep_raw=args.keep_raw,
                    emit_only=args.emit_only,
                    provenance=provenance,
                )
                records.append(record)
                summary = {
                    "case": case,
                    "mode": mode,
                    "variant": variant,
                    "status": record["status"],
                    "verdict": record["verdict"],
                    "directions": {
                        direction: record["directions"][direction]["judgement"]["verdict"]
                        for direction in DIRECTIONS
                    }
                    if "directions" in record
                    else None,
                    "reason": record.get("reason"),
                }
                print(json.dumps(summary), flush=True)
    by_key = {(record["case"], record["mode"], record["variant"]): record for record in records}
    controls = []
    for case in CASES:
        for mode in MODES:
            clean = by_key[(case, mode, "clean")]
            fault = by_key[(case, mode, "fault")]
            for direction in DIRECTIONS:
                clean_verdict = (
                    clean["directions"][direction]["judgement"]["verdict"]
                    if "directions" in clean
                    else clean["verdict"]
                )
                fault_verdict = (
                    fault["directions"][direction]["judgement"]["verdict"]
                    if "directions" in fault
                    else fault["verdict"]
                )
                controls.append(
                    {
                        "case": case,
                        "mode": mode,
                        "direction": direction,
                        "status": "DECK_ONLY"
                        if args.emit_only
                        else "OK"
                        if clean_verdict == "PASS" and fault_verdict == "FAIL"
                        else "VIOLATED",
                        "clean_verdict": clean_verdict,
                        "fault_verdict": fault_verdict,
                    }
                )
    manifest = {
        "schema_version": 1,
        "provenance": provenance,
        "expected_cases": list(CASES),
        "expected_modes": list(MODES),
        "expected_variants": list(VARIANTS),
        "expected_directions": list(DIRECTIONS),
        "runs": records,
        "controls": controls,
        "measured": sum(record["status"] == "MEASURED" for record in records),
        "unknown": sum(record["status"] == "UNKNOWN" for record in records),
        "failed": sum(record["status"] == "RUN_FAILED" for record in records),
        "direction_unknown": sum(
            record["directions"][direction]["judgement"]["verdict"] == "UNKNOWN"
            for record in records
            if "directions" in record
            for direction in DIRECTIONS
        ),
        "control_violations": sum(control["status"] == "VIOLATED" for control in controls),
    }
    _write_json(out / "manifest.json", manifest)
    return int(
        manifest["failed"] > 0
        or manifest["unknown"] > 0
        or manifest["direction_unknown"] > 0
        or manifest["control_violations"] > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
