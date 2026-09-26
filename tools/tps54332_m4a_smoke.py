r"""Run matched SW/AVG TPS54332 smoke benches from the frozen local spec.

This is a small electrical smoke check, not the complete M4 acceptance suite.
It uses the M2 startup and controlled-COMP gain circuits, preserving their
passives and stimuli in both modes. Electrical results require LTspice raw/log
artifacts; a rendered deck or parameter value alone is never a PASS.

Example (PowerShell)::

    .\.venv\Scripts\python.exe tools\tps54332_m4a_smoke.py --ltspice-exe "C:\path\to\LTspice.exe" --requirements models\T1-tps54332\spec\requirements.json --bindings models\T1-tps54332\spec\bindings.json --out runs\m4a-smoke
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
    build_buck_system_benches,
    render_deck,
)
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.buck_switching import BuckSeed, seed_from_spec
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import RawFile, read_raw

REPO = Path(__file__).resolve().parents[1]
PART = "TPS54332DDA"
CASES = ("startup", "gain")
MODES = ("SW", "AVG")
FROZEN_REQUIREMENTS = REPO / "models/T1-tps54332/spec/requirements.json"
FROZEN_BINDINGS = REPO / "models/T1-tps54332/spec/bindings.json"
REQUIREMENTS_SHA256 = "3884fd76976e071cd2ea81d5db64cb1bde17aecbaccc8f87399e7448646cc060"
BINDINGS_SHA256 = "1b3d45e8977948ce2ed7f1079db2aaa598b543cd65a83b697af8455d6345ad57"
SPEC_DIGEST = "499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7"
SW_LIBRARY_SHA256 = "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2"
AVG_MAX_STEP_S = 1e-6


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
    parser.add_argument(
        "--emit-only", action="store_true", help="Render and check decks without LTspice"
    )
    return parser.parse_args()


def _validate(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    exe = args.ltspice_exe.resolve(strict=True)
    requirements = args.requirements.resolve(strict=True)
    bindings = args.bindings.resolve(strict=True)
    out = args.out.resolve()
    if not exe.is_file() or not requirements.is_file() or not bindings.is_file():
        raise ValueError("LTspice, requirements, and bindings must be explicit files")
    if requirements != FROZEN_REQUIREMENTS.resolve() or bindings != FROZEN_BINDINGS.resolve():
        raise ValueError("M4a smoke accepts only the frozen local TPS54332 inputs")
    if _sha256(requirements) != REQUIREMENTS_SHA256 or _sha256(bindings) != BINDINGS_SHA256:
        raise ValueError("frozen TPS54332 requirements or bindings changed")
    if not out.is_relative_to((REPO / "runs").resolve()):
        raise ValueError("--out must be inside this repository's ignored runs/ directory")
    if len(str(out)) > 125:
        raise ValueError("Choose a shorter --out path for LTspice on Windows")
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be empty so records cannot mix with a prior run")
    if not math.isfinite(args.timeout_s) or args.timeout_s <= 0:
        raise ValueError("--timeout-s must be a positive finite number")
    return exe, requirements, bindings, out


def _check_origins(seed: BuckSeed, requirements: Path, doc_id: str) -> None:
    source = json.loads(requirements.read_text(encoding="utf-8"))
    rows = {row["req_id"]: row for row in source["requirements"]}
    cited = 0
    for item in seed.parameters:
        if item.origin not in {"cited_row", "derived_from_bounds"}:
            continue
        if item.row_id is None or item.page is None or not item.excerpt:
            raise ValueError(f"{item.name}: cited parameter has incomplete origin")
        row = rows.get(item.row_id)
        evidence = row.get("evidence") if row else None
        if (
            row is None
            or row.get("citation_verified") is not True
            or not evidence
            or evidence[0].get("doc_id") != doc_id
            or evidence[0].get("excerpt") != item.excerpt
            or evidence[0].get("page", {}).get("pdf_page") != item.page
        ):
            raise ValueError(f"{item.name}: origin differs from verified frozen source")
        cited += 1
    if cited < 10:
        raise ValueError("TPS54332 seed lost most cited parameter origins")


def _deck(bench: BuckBench, model: Path, mode: str, inductance_h: float) -> tuple[str, float]:
    max_step = bench.max_step_s if mode == "SW" else AVG_MAX_STEP_S
    signals = tuple(
        dict.fromkeys((*bench.saved_signals, "V(out)", "V(ss)", "I(Lout)", "V(vin)", "I(Vin)"))
    )
    checked = replace(bench, max_step_s=max_step, saved_signals=signals)
    lines = render_deck(checked, model).splitlines()
    instance_lines = [index for index, line in enumerate(lines) if line.startswith("XU1 ")]
    if len(instance_lines) != 1:
        raise ValueError("expected one TPS54332 XU1 instance in the M2 deck")
    if mode == "AVG":
        lines[instance_lines[0]] += f" L_EXT={inductance_h:.12g}"
    lines[0] = f"* Deterministic M4a {mode} {bench.name}; no electrical verdict in deck"
    return "\n".join(lines) + "\n", max_step


def _signals(raw: RawFile) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    t_raw = raw.time_column()
    if t_raw is None:
        raise ValueError("raw waveform has no time axis")
    t = np.asarray(t_raw, dtype=float)
    names = ("V(out)", "V(ss)", "I(Lout)", "V(vin)", "I(Vin)")
    signals = {name: np.asarray(raw.column(name), dtype=float) for name in names}
    if len(t) < 3 or not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ValueError("raw waveform has a nonfinite or decreasing time axis")
    if any(len(signal) != len(t) or not np.all(np.isfinite(signal)) for signal in signals.values()):
        raise ValueError(
            "raw waveform has a missing or nonfinite output, SS, inductor or input current"
        )
    # LTspice can repeat event times. Retain the last value at each repeated time.
    keep = np.r_[np.diff(t) > 0, True]
    t = t[keep]
    if len(t) < 3:
        raise ValueError("raw waveform has fewer than three distinct times")
    return t, {name: signal[keep] for name, signal in signals.items()}


def _window_stats(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> dict[str, float]:
    if start < t[0] or end > t[-1] or start >= end:
        raise ValueError(f"waveform does not cover {start:g}..{end:g} s")
    interior = (t > start) & (t < end)
    times = np.r_[start, t[interior], end]
    values = np.interp(times, t, signal)
    if len(times) < 3:
        raise ValueError(f"waveform window {start:g}..{end:g} s has no interior point")
    return {
        "mean": float(np.trapezoid(values, times) / (end - start)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def _window_integral(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> float:
    if start < t[0] or end > t[-1] or start >= end:
        raise ValueError(f"waveform does not cover {start:g}..{end:g} s")
    interior = (t > start) & (t < end)
    times = np.r_[start, t[interior], end]
    values = np.interp(times, t, signal)
    return float(np.trapezoid(values, times))


def _measure(bench: BuckBench, raw: RawFile) -> dict[str, Any]:
    t, signals = _signals(raw)
    tail = min(0.5e-3, bench.stop_s - bench.window_s[0])
    final_start = bench.stop_s - tail
    summary: dict[str, Any] = {
        "raw_points": raw.npoints,
        "distinct_time_points": len(t),
        "final_window_s": [final_start, bench.stop_s],
        "final_output_v": _window_stats(t, signals["V(out)"], final_start, bench.stop_s),
        "final_ss_v": _window_stats(t, signals["V(ss)"], final_start, bench.stop_s),
        "final_inductor_a": _window_stats(t, signals["I(Lout)"], final_start, bench.stop_s),
    }
    input_power = -signals["V(vin)"] * signals["I(Vin)"]
    summary["input_source"] = {
        "status": "UNJUDGED",
        "positive_power_convention": "-V(vin)*I(Vin); positive means source delivers power",
        "final_power_w": _window_stats(t, input_power, final_start, bench.stop_s),
        "energy_0_to_stop_j": _window_integral(t, input_power, 0.0, bench.stop_s),
        "scope": "fixture source includes input capacitor charging; no efficiency verdict",
    }
    if bench.kind == "startup":
        out = signals["V(out)"]
        final_v = summary["final_output_v"]["mean"]
        if final_v <= 0:
            raise ValueError("startup output did not reach a positive voltage")
        t10 = _first_crossing(t, out, 0.1 * final_v)
        t90 = _first_crossing(t, out, 0.9 * final_v)
        if t10 is None or t90 is None or t90 <= t10:
            raise ValueError("startup output has no ordered 10/90% crossings")
        summary["rise_10_90_s"] = t90 - t10
        return summary
    current = signals["I(Lout)"]
    comp = np.asarray(raw.column("V(comp)"), dtype=float)
    if len(comp) != raw.npoints or not np.all(np.isfinite(comp)):
        raise ValueError("gain waveform has missing or nonfinite COMP")
    comp = comp[np.r_[np.diff(np.asarray(raw.time_column(), dtype=float)) > 0, True]]
    points = []
    for point in bench.gain_points:
        early = _window_stats(t, current, *point.early_window_s)
        late = _window_stats(t, current, *point.late_window_s)
        comp_late = _window_stats(t, comp, *point.late_window_s)
        points.append(
            {
                "requested_comp_v": point.comp_v,
                "observed_comp_v": comp_late["mean"],
                "early_inductor_mean_a": early["mean"],
                "late_inductor_mean_a": late["mean"],
                "early_late_delta_a": abs(late["mean"] - early["mean"]),
                "stable": abs(late["mean"] - early["mean"]) <= max(0.05, 0.05 * abs(late["mean"])),
            }
        )
    summary["gain_points"] = points
    summary["gain_method"] = "time-weighted mean inductor current in predeclared M2 COMP windows"
    return summary


def _smoke_startup(bench: BuckBench, measurements: dict[str, Any]) -> dict[str, Any]:
    """Synthetic settling guards, not datasheet limits or a VREF verdict."""
    target = float(bench.metadata["vout_target_v"])
    if not math.isfinite(target) or target <= 0:
        raise ValueError("startup bench has no positive expected output")
    lower, upper = 0.98 * target, 1.02 * target
    final = measurements["final_output_v"]
    final_pp = final["max"] - final["min"]
    checks = [
        {
            "id": "final_mean_in_target_band",
            "measured_v": final["mean"],
            "lower_v": lower,
            "upper_v": upper,
            "status": "OK" if lower <= final["mean"] <= upper else "VIOLATED",
        },
        {
            "id": "all_final_samples_in_target_band",
            "measured_min_v": final["min"],
            "measured_max_v": final["max"],
            "lower_v": lower,
            "upper_v": upper,
            "status": "OK" if lower <= final["min"] <= final["max"] <= upper else "VIOLATED",
        },
        {
            "id": "final_peak_to_peak_settling",
            "measured_v": final_pp,
            "upper_v": 0.02 * target,
            "status": "OK" if final_pp <= 0.02 * target else "VIOLATED",
        },
    ]
    return {
        "status": "OK" if all(check["status"] == "OK" for check in checks) else "VIOLATED",
        "basis": "synthetic_fixture_only; not datasheet limits",
        "target_v": target,
        "target_origin": "M2 bench cited VREF typical and external feedback divider",
        "window_s": measurements["final_window_s"],
        "checks": checks,
    }


def _smoke_avg_gain(
    bench: BuckBench, measurements: dict[str, Any], nominal_ilim_a: float
) -> dict[str, Any]:
    """Check active current response and saturation without claiming GMCS accuracy."""
    veco = float(bench.metadata["veco_v"])
    points = measurements["gain_points"]
    active = [
        point
        for point in points
        if veco + 0.05 - 1e-8 <= point["requested_comp_v"] <= veco + 0.40 + 1e-8
    ]
    capped = [point for point in points if point["requested_comp_v"] >= veco + 0.50 - 1e-8]
    active_currents = [point["late_inductor_mean_a"] for point in active]
    capped_currents = [point["late_inductor_mean_a"] for point in capped]
    increments = np.diff(active_currents).tolist() if len(active_currents) >= 2 else []
    active_stable = len(active) >= 5 and all(point["stable"] for point in active)
    monotonic = len(active) >= 5 and all(delta >= 0.1 for delta in increments)
    cap_range = len(capped) >= 2 and all(
        0.8 * nominal_ilim_a <= value <= 1.1 * nominal_ilim_a for value in capped_currents
    )
    cap_span = max(capped_currents) - min(capped_currents) if capped_currents else None
    cap_flat = cap_span is not None and cap_span <= 0.05 * nominal_ilim_a
    all_below_cap = all(point["late_inductor_mean_a"] <= 1.1 * nominal_ilim_a for point in points)
    checks = [
        {
            "id": "active_points_stable",
            "point_count": len(active),
            "stable_count": sum(point["stable"] for point in active),
            "required_count": 5,
            "status": "OK" if active_stable else "VIOLATED",
        },
        {
            "id": "active_current_monotonic",
            "increments_a": increments,
            "minimum_increment_a": 0.1,
            "status": "OK" if monotonic else "VIOLATED",
        },
        {
            "id": "high_comp_current_near_nominal_limit",
            "currents_a": capped_currents,
            "lower_a": 0.8 * nominal_ilim_a,
            "upper_a": 1.1 * nominal_ilim_a,
            "status": "OK" if cap_range else "VIOLATED",
        },
        {
            "id": "high_comp_current_plateau",
            "span_a": cap_span,
            "upper_a": 0.05 * nominal_ilim_a,
            "status": "OK" if cap_flat else "VIOLATED",
        },
        {
            "id": "all_points_below_cap_guard",
            "max_a": max(point["late_inductor_mean_a"] for point in points),
            "upper_a": 1.1 * nominal_ilim_a,
            "status": "OK" if all_below_cap else "VIOLATED",
        },
    ]
    return {
        "status": "OK" if all(check["status"] == "OK" for check in checks) else "VIOLATED",
        "basis": "synthetic_fixture_only; not datasheet GMCS or ILIM acceptance",
        "active_comp_range_v": [veco + 0.05, veco + 0.40],
        "cap_comp_min_v": veco + 0.50,
        "nominal_seed_ilim_a": nominal_ilim_a,
        "checks": checks,
    }


def _smoke(
    bench: BuckBench, mode: str, measurements: dict[str, Any], nominal_ilim_a: float
) -> dict[str, Any]:
    if bench.kind == "startup":
        return _smoke_startup(bench, measurements)
    if mode == "AVG":
        return _smoke_avg_gain(bench, measurements, nominal_ilim_a)
    return {
        "status": "NOT_APPLICABLE",
        "basis": "synthetic_fixture_only",
        "reason": "SW gain requires cycle-peak analysis; this smoke records mean current only",
    }


def _first_crossing(t: np.ndarray, signal: np.ndarray, level: float) -> float | None:
    indexes = np.flatnonzero((signal[:-1] < level) & (signal[1:] >= level))
    if not len(indexes):
        return None
    index = int(indexes[0])
    fraction = (level - signal[index]) / (signal[index + 1] - signal[index])
    return float(t[index] + fraction * (t[index + 1] - t[index]))


def _run_one(
    *,
    bench: BuckBench,
    mode: str,
    exe: Path,
    model: Path,
    out: Path,
    inductance_h: float,
    timeout_s: float,
    keep_raw: bool,
    emit_only: bool,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    folder = out / f"{bench.name}-{mode.lower()}"
    folder.mkdir(parents=True, exist_ok=True)
    deck = folder / f"{bench.name}-{mode.lower()}.cir"
    deck_text, max_step = _deck(bench, model, mode, inductance_h)
    deck.write_text(deck_text, encoding="utf-8", newline="\n")
    record: dict[str, Any] = {
        "schema_version": 1,
        "case": bench.name,
        "mode": mode,
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
        "source_rows": [asdict(row) for row in bench.source_rows],
        "passive_source": bench.passive_source,
        "bench_components": list(bench.components),
        "bench_window_s": bench.window_s,
        "max_step_s": max_step,
        "l_ext_h": inductance_h if mode == "AVG" else None,
        "smoke": {
            "status": "NOT_APPLICABLE" if emit_only else "UNKNOWN",
            "basis": "synthetic_fixture_only; not datasheet limits",
            "reason": "No LTspice waveform was requested" if emit_only else "measurement pending",
        },
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
        if run.ok and run.raw_path:
            try:
                record["measurements"] = _measure(bench, read_raw(run.raw_path))
                record["status"] = "MEASURED"
                record["reason"] = None
                record["smoke"] = _smoke(
                    bench, mode, record["measurements"], provenance["nominal_seed_ilim_a"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                record["status"] = "UNKNOWN"
                record["reason"] = f"waveform measurement unavailable: {type(exc).__name__}: {exc}"
                record["smoke"]["reason"] = record["reason"]
        else:
            record["reason"] = "LTspice did not finish with a readable raw/log waveform"
            record["smoke"]["reason"] = record["reason"]
    except Exception as exc:
        record["elapsed_s"] = round(time.perf_counter() - started, 3)
        record["reason"] = f"{type(exc).__name__}: {exc}"
        record["smoke"]["reason"] = record["reason"]
    finally:
        if run is not None and not keep_raw:
            for artifact in (run.raw_path, run.op_raw_path):
                if artifact is not None:
                    artifact.unlink(missing_ok=True)
        record["raw_retained"] = bool(run is not None and run.raw_path and keep_raw)
        _write_json(folder / "result.json", record)
    return record


def _speed_pairs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(record["case"], record["mode"]): record for record in records}
    pairs = []
    for case in CASES:
        sw, avg = by_key[(case, "SW")], by_key[(case, "AVG")]
        sw_wall = sw.get("ltspice_wall_s")
        avg_wall = avg.get("ltspice_wall_s")
        comparable = sw["status"] == avg["status"] == "MEASURED" and sw_wall and avg_wall
        pairs.append(
            {
                "case": case,
                "sw_wall_s": sw_wall,
                "avg_wall_s": avg_wall,
                "sw_over_avg": round(sw_wall / avg_wall, 3) if comparable else None,
                "status": "MEASURED" if comparable else "UNKNOWN",
                "reason": None
                if comparable
                else "both matched waveforms and wall times are required",
            }
        )
    return pairs


def main() -> int:
    args = _args()
    exe, requirements, bindings, out = _validate(args)
    spec = load_tps54320_spec(requirements, bindings, part=PART, subckt=PART)
    if spec.digest() != SPEC_DIGEST:
        raise ValueError("frozen TPS54332 spec digest changed")
    sw = seed_from_spec(spec, mode="SW")
    avg = seed_from_spec(spec, mode="AVG")
    if sw is None or avg is None or sw.part != PART or avg.part != PART:
        raise ValueError("frozen TPS54332 specification did not render both modes")
    if sw.ports != avg.ports or sw.parameters != avg.parameters:
        raise ValueError("SW/AVG physical ports or cited-device parameter origins differ")
    _check_origins(sw, requirements, spec.doc_id)
    parts = BuckBenchParts()
    benches = build_buck_system_benches(spec, parts, requirements_path=requirements)
    by_name = {bench.name: bench for bench in benches}
    if not all(name in by_name for name in CASES):
        raise ValueError("M2 bench builder did not provide startup and gain")
    if any(by_name[name].ports != sw.ports for name in CASES):
        raise ValueError("M2 bench physical ports differ from the rendered models")
    out.mkdir(parents=True, exist_ok=True)
    models = {"SW": out / "sw-model.lib", "AVG": out / "avg-model.lib"}
    sw.write(models["SW"])
    avg.write(models["AVG"])
    model_hashes = {mode: _sha256(path) for mode, path in models.items()}
    if model_hashes["SW"] != SW_LIBRARY_SHA256:
        raise ValueError("SW library differs from the frozen M3 rendered model")
    if model_hashes["AVG"] == model_hashes["SW"]:
        raise ValueError("AVG rendered identical switching library bytes")
    if ".param L_EXT=" not in avg.library_text:
        raise ValueError("AVG model does not declare an instance L_EXT parameter")
    provenance: dict[str, Any] = {
        "schema_version": 1,
        "part": PART,
        "spec_digest": spec.digest(),
        "requirements_sha256": _sha256(requirements),
        "bindings_sha256": _sha256(bindings),
        "ltspice_exe_sha256": _sha256(exe),
        "script_sha256": _sha256(Path(__file__)),
        "model_sha256": model_hashes,
        "contract_sha256": sw.contract_sha256,
        "physical_ports": list(sw.ports),
        "shared_parameter_origins": [item.payload() for item in sw.parameters],
        "external_bench_inductance_h": parts.inductance_h,
        "avg_max_step_s": AVG_MAX_STEP_S,
        "nominal_seed_ilim_a": next(item.value for item in sw.parameters if item.name == "ILIM"),
        "verdict": "UNJUDGED; smoke measurements are not full M4 acceptance",
    }
    _write_json(out / "provenance.json", provenance)
    records = []
    for name in CASES:
        for mode in MODES:
            record = _run_one(
                bench=by_name[name],
                mode=mode,
                exe=exe,
                model=models[mode],
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
                        "case": name,
                        "mode": mode,
                        "status": record["status"],
                        "reason": record.get("reason"),
                    }
                ),
                flush=True,
            )
    manifest = {
        "schema_version": 1,
        "provenance": provenance,
        "expected_cases": list(CASES),
        "expected_modes": list(MODES),
        "runs": records,
        "speed_pairs": _speed_pairs(records),
        "measured": sum(record["status"] == "MEASURED" for record in records),
        "unknown": sum(record["status"] == "UNKNOWN" for record in records),
        "failed": sum(record["status"] == "RUN_FAILED" for record in records),
        "deck_only": sum(record["status"] == "DECK_ONLY" for record in records),
        "smoke_violations": sum(record["smoke"]["status"] == "VIOLATED" for record in records),
        "smoke_unknown": sum(record["smoke"]["status"] == "UNKNOWN" for record in records),
    }
    _write_json(out / "manifest.json", manifest)
    return int(
        manifest["failed"] > 0
        or manifest["unknown"] > 0
        or manifest["smoke_violations"] > 0
        or manifest["smoke_unknown"] > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
