r"""Qualify four cited TPS54332 values in switching and averaged modes.

All eight clean/fault pairs are code-built from the frozen local TPS spec and
the M2 physical application. A zero-volt shunt after the source-side input
capacitor measures current into the DUT alone. Results require LTspice raw and
log artifacts; parameter declarations and generated decks never prove a PASS.

Example (PowerShell)::

    .\.venv\Scripts\python.exe tools\tps54332_m4b1_verify.py --ltspice-exe "C:\path\to\LTspice.exe" --requirements models\T1-tps54332\spec\requirements.json --bindings models\T1-tps54332\spec\bindings.json --out runs\m4b1
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
CASES = ("vref", "ss_charge", "shutdown_iq", "operating_iq")
VARIANTS = ("clean", "fault")
FROZEN_REQUIREMENTS = REPO / "models/T1-tps54332/spec/requirements.json"
FROZEN_BINDINGS = REPO / "models/T1-tps54332/spec/bindings.json"
REQUIREMENTS_SHA256 = "3884fd76976e071cd2ea81d5db64cb1bde17aecbaccc8f87399e7448646cc060"
BINDINGS_SHA256 = "1b3d45e8977948ce2ed7f1079db2aaa598b543cd65a83b697af8455d6345ad57"
SPEC_DIGEST = "499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7"
SW_LIBRARY_SHA256 = "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2"
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
        raise ValueError("M4b1 requires the frozen local TPS54332 inputs")
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
    deck_text, max_step = _deck(bench, model, mode, variant, inductance_h)
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
        "source_rows_in_deck": [asdict(source_row) for source_row in bench.source_rows],
        "bench_components": list(bench.components),
        "bench_metadata": bench.metadata,
        "window_s": bench.window_s,
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
                measurement = _measure(case, mode, bench, row, read_raw(run.raw_path))
                judgement = _judge(row, measurement)
                record.update(
                    status="MEASURED",
                    measurement=measurement,
                    verdict=judgement["verdict"],
                    judgement=judgement,
                    reason=judgement["reason"],
                )
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
        raise ValueError("SW/AVG physical ports or device-parameter origins differ")
    rows = _cited_rows(spec, requirements)
    parts = BuckBenchParts()
    m2 = build_buck_system_benches(spec, parts, requirements_path=requirements)
    startup = next((bench for bench in m2 if bench.name == "startup"), None)
    if startup is None or startup.ports != sw.ports:
        raise ValueError("M2 startup application or physical-port contract changed")
    benches = {case: _case_bench(startup, case, rows, parts) for case in CASES}
    out.mkdir(parents=True, exist_ok=True)
    models = {"SW": out / "sw-model.lib", "AVG": out / "avg-model.lib"}
    sw.write(models["SW"])
    avg.write(models["AVG"])
    hashes = {mode: _sha256(model) for mode, model in models.items()}
    if hashes["SW"] != SW_LIBRARY_SHA256 or hashes["AVG"] == hashes["SW"]:
        raise ValueError("frozen SW hash changed or AVG rendered as SW")
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
        "verdict_scope": "four cited nominal-25C waveform checks; not all M4b or process corners",
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
                print(
                    json.dumps(
                        {
                            "case": case,
                            "mode": mode,
                            "variant": variant,
                            "status": record["status"],
                            "verdict": record["verdict"],
                            "reason": record.get("reason"),
                        }
                    ),
                    flush=True,
                )
    controls = []
    by_key = {(record["case"], record["mode"], record["variant"]): record for record in records}
    for case in CASES:
        for mode in MODES:
            clean = by_key[(case, mode, "clean")]
            fault = by_key[(case, mode, "fault")]
            controls.append(
                {
                    "case": case,
                    "mode": mode,
                    "status": "DECK_ONLY"
                    if args.emit_only
                    else "OK"
                    if clean["verdict"] == "PASS" and fault["verdict"] == "FAIL"
                    else "VIOLATED",
                    "clean_verdict": clean["verdict"],
                    "fault_verdict": fault["verdict"],
                }
            )
    manifest = {
        "schema_version": 1,
        "provenance": provenance,
        "expected_cases": list(CASES),
        "expected_modes": list(MODES),
        "expected_variants": list(VARIANTS),
        "runs": records,
        "controls": controls,
        "measured": sum(record["status"] == "MEASURED" for record in records),
        "unknown": sum(record["status"] == "UNKNOWN" for record in records),
        "failed": sum(record["status"] == "RUN_FAILED" for record in records),
        "control_violations": sum(control["status"] == "VIOLATED" for control in controls),
    }
    _write_json(out / "manifest.json", manifest)
    return int(
        manifest["failed"] > 0 or manifest["unknown"] > 0 or manifest["control_violations"] > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
