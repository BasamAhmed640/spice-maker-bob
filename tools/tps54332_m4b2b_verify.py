r"""Check TPS54332 COMP gain and current-limit template behavior in LTspice.

The test circuit is built from the frozen, citation-checked M2 application.
Exactly three post-soft-start COMP levels (0.80, 0.85, 0.90 V) form the gain
fit. SW uses external inductor cycle peaks; AVG uses the external inductor
time-weighted mean. Resistive overload is switched in after soft start. Its SW
cycle peaks can check the selected template ILIM setting, with a separately
declared 0.10 A dynamic allowance. AVG inductor means cannot prove a peak
threshold, so AVG limit verdicts remain UNKNOWN even with a measured waveform.

The wrong-value controls are GMCS=8 A/V and ILIM=8 A. This test neither
claims silicon process corners nor repairs historical low-COMP gain behavior.
No electrical PASS is possible without hashed LTspice raw and log artifacts.

PowerShell example::

    .\.venv\Scripts\python.exe tools\tps54332_m4b2b_verify.py --ltspice-exe "C:\path\to\LTspice.exe" --requirements models\T1-tps54332\spec\requirements.json --bindings models\T1-tps54332\spec\bindings.json --out runs\m4b2b
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
    GainPoint,
    _check_verified_row,
    _cycle_peaks,
    _gain_waveform,
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
CASES = ("gain", "limit_min", "limit_max")
VARIANTS = ("clean", "fault")
FROZEN_REQUIREMENTS = REPO / "models/T1-tps54332/spec/requirements.json"
FROZEN_BINDINGS = REPO / "models/T1-tps54332/spec/bindings.json"
REQUIREMENTS_SHA256 = "3884fd76976e071cd2ea81d5db64cb1bde17aecbaccc8f87399e7448646cc060"
BINDINGS_SHA256 = "1b3d45e8977948ce2ed7f1079db2aaa598b543cd65a83b697af8455d6345ad57"
SPEC_DIGEST = "499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7"
REVIEWED_MODEL_SHA256 = {
    "SW": "df4abecd09c5642360499450ae8123b51ef8e75beccc9df03832fec189da948f",
    "AVG": "718b763aa006de166b6807536861026a49892525047c163ae30fcdaf5bdb525c",
}
ROW_IDS = {
    "gain": "B002_TPS54332DDA_SW_CURRENT_TO_COMP",
    "limit_min": "B002_TPS54332DDA_ILIM",
    "limit_max": "B002_TPS54332DDA_ILIM",
    "veco": "B003_REQ_TPS54332_ECOMODE_COMP_0P5V",
}
GAIN_LEVELS_V = (0.80, 0.85, 0.90)
GAIN_HOLD_S = 500e-6
GAIN_TYPICAL_FRACTION = 0.10  # Synthetic engineering guard; citation gives typical only.
LIMIT_DYNAMIC_ALLOWANCE_A = 0.10  # Synthetic waveform/turn-off allowance, not a TI bound.
LIMIT_COLLAPSE_FRACTION = 0.05  # Synthetic guard that post-fault VOUT falls from its own baseline.
WRONG_GMCS_A_PER_V = 8.0
WRONG_ILIM_A = 8.0
SW_MAX_STEP_S = 50e-9
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
        raise ValueError("M4b2b requires the frozen local TPS54332 inputs")
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
    if result["gain"].typ_value is None or result["gain"].typ_value <= 0:
        raise ValueError("cited current-to-COMP gain lacks a positive typical value")
    if result["veco"].typ_value is None or result["veco"].typ_value >= min(GAIN_LEVELS_V):
        raise ValueError("gain levels must be strictly above cited Eco threshold")
    if (
        result["limit_min"].min_value is None
        or result["limit_max"].max_value is None
        or result["limit_min"].min_value >= result["limit_max"].max_value
    ):
        raise ValueError("cited current-limit bounds are unavailable or unordered")
    return result


def _gain_bench(base: BuckBench) -> BuckBench:
    if base.name != "gain" or len(base.gain_points) < 3:
        raise ValueError("M2 gain fixture is missing")
    gain_start = base.gain_points[0].early_window_s[0] - 150e-6
    points = tuple(
        GainPoint(
            level,
            (gain_start + index * GAIN_HOLD_S + 150e-6, gain_start + index * GAIN_HOLD_S + 300e-6),
            (
                gain_start + index * GAIN_HOLD_S + 350e-6,
                gain_start + (index + 1) * GAIN_HOLD_S - 5e-6,
            ),
            f"gain_p{index + 1:02d}",
        )
        for index, level in enumerate(GAIN_LEVELS_V)
    )
    components = list(base.components)
    forces = [index for index, card in enumerate(components) if card.startswith("Vforce ")]
    if len(forces) != 1 or not any(card.startswith("Rgain ") for card in components):
        raise ValueError("M2 gain fixture requires forced COMP and resistive output load")
    components[forces[0]] = _gain_waveform(GAIN_LEVELS_V, gain_start, GAIN_HOLD_S)
    return replace(
        base,
        components=tuple(components),
        measures=(),
        gain_points=points,
        stop_s=gain_start + len(points) * GAIN_HOLD_S + 50e-6,
        window_s=(gain_start, gain_start + len(points) * GAIN_HOLD_S),
        metadata={
            **base.metadata,
            "prespecified_comp_v": GAIN_LEVELS_V,
            "gain_typical_fraction": GAIN_TYPICAL_FRACTION,
            "low_comp_historic_defect": "not exercised; no claim of repair",
        },
    )


def _deck(
    bench: BuckBench, model: Path, mode: str, variant: str, inductance_h: float
) -> tuple[str, float]:
    if mode not in MODES or variant not in VARIANTS:
        raise ValueError("unrecognized M4b2b mode or variant")
    max_step = SW_MAX_STEP_S if mode == "SW" else AVG_MAX_STEP_S
    saved = ("V(vin)", "V(en)", "V(ss)", "V(vsense)", "V(out)", "V(ph)", "V(comp)", "I(Lout)")
    checked = replace(bench, saved_signals=saved, max_step_s=max_step, measures=())
    lines = render_deck(checked, model).splitlines()
    instances = [index for index, line in enumerate(lines) if line.startswith("XU1 ")]
    if len(instances) != 1:
        raise ValueError("expected exactly one TPS54332 instance")
    instance = lines[instances[0]]
    if mode == "AVG":
        instance += f" L_EXT={inductance_h:.12g}"
    if bench.kind == "gain":
        if "ILIM=" in instance:
            raise ValueError("gain fixture unexpectedly overrides ILIM")
        if variant == "fault":
            instance += f" GMCS={WRONG_GMCS_A_PER_V:.12g}"
    else:
        expected = f"ILIM={bench.ilim_instance_a:.12g}"
        if instance.count(expected) != 1:
            raise ValueError("resistive overload fixture lacks cited ILIM override")
        if variant == "fault":
            instance = instance.replace(expected, f"ILIM={WRONG_ILIM_A:.12g}")
    lines[instances[0]] = instance
    lines[0] = f"* M4b2b {bench.name} {mode} {variant}; raw/log needed for a verdict"
    tran = [index for index, line in enumerate(lines) if line.startswith(".tran ")]
    if len(tran) != 1:
        raise ValueError("expected exactly one transient command")
    lines.insert(tran[0], ".options plotwinsize=0")
    return "\n".join(lines) + "\n", max_step


def _traces(raw: RawFile) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    axis = raw.time_column()
    if axis is None:
        raise ValueError("raw waveform has no time axis")
    t = np.asarray(axis, dtype=float)
    names = ("V(vin)", "V(en)", "V(ss)", "V(vsense)", "V(out)", "V(ph)", "V(comp)", "I(Lout)")
    signals = {name: np.asarray(raw.column(name), dtype=float) for name in names}
    if len(t) < 3 or not np.all(np.isfinite(t)) or np.any(np.diff(t) < 0):
        raise ValueError("raw waveform has nonfinite or decreasing time")
    if any(len(value) != len(t) or not np.all(np.isfinite(value)) for value in signals.values()):
        raise ValueError("raw waveform has missing or nonfinite saved signals")
    keep = np.r_[np.diff(t) > 0, True]
    t = t[keep]
    if len(t) < 3:
        raise ValueError("raw waveform has fewer than three distinct times")
    return t, {name: value[keep] for name, value in signals.items()}


def _stats(t: np.ndarray, signal: np.ndarray, start: float, end: float) -> dict[str, float]:
    if start < t[0] or end > t[-1] or end <= start:
        raise ValueError(f"waveform does not cover {start:g}..{end:g} s")
    inside = (t > start) & (t < end)
    times = np.r_[start, t[inside], end]
    if len(times) < 3:
        raise ValueError("window has no interior waveform point")
    values = np.interp(times, t, signal)
    return {
        "mean": float(np.trapezoid(values, times) / (end - start)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "npoints": len(times),
    }


def _measure_gain(mode: str, bench: BuckBench, raw: RawFile) -> dict[str, Any]:
    t, signals = _traces(raw)
    points: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    for index, point in enumerate(bench.gain_points):
        early_start, early_end = point.early_window_s
        late_start, late_end = point.late_window_s
        comp = _stats(t, signals["V(comp)"], late_start, late_end)
        vin = _stats(t, signals["V(vin)"], late_start, late_end)
        ss = _stats(t, signals["V(ss)"], late_start, late_end)
        out = _stats(t, signals["V(out)"], late_start, late_end)
        if mode == "SW":
            early, early_cycles, _ = _cycle_peaks(
                t,
                signals["V(ph)"],
                signals["I(Lout)"],
                early_start,
                early_end,
                0.5 * bench.vin_v,
            )
            late, cycles, maximum = _cycle_peaks(
                t,
                signals["V(ph)"],
                signals["I(Lout)"],
                late_start,
                late_end,
                0.5 * bench.vin_v,
            )
            resolved = early is not None and late is not None and min(early_cycles, cycles) >= 10
            method = "external Lout median complete-cycle peak"
        else:
            early_stats = _stats(t, signals["I(Lout)"], early_start, early_end)
            late_stats = _stats(t, signals["I(Lout)"], late_start, late_end)
            early, late = early_stats["mean"], late_stats["mean"]
            early_cycles = cycles = None
            maximum = late_stats["max"]
            resolved = min(early_stats["npoints"], late_stats["npoints"]) >= 20
            method = "external Lout time-weighted mean; AVG has no switching peaks"
        stable = (
            resolved
            and early is not None
            and late is not None
            and abs(late - early) <= max(0.05, 0.05 * abs(late))
        )
        checks[f"p{index + 1:02d}_vin_12v"] = vin["min"] >= 11.9 and vin["max"] <= 12.1
        checks[f"p{index + 1:02d}_comp_forced"] = (
            comp["min"] >= point.comp_v - 0.005 and comp["max"] <= point.comp_v + 0.005
        )
        checks[f"p{index + 1:02d}_ss_settled"] = ss["min"] > 0.828
        checks[f"p{index + 1:02d}_output_active"] = out["mean"] > 0.5 and (late or 0) > 0.5
        checks[f"p{index + 1:02d}_current_settled"] = stable
        points.append(
            {
                "comp_set_v": point.comp_v,
                "comp_observed_v": comp,
                "vin_v": vin,
                "ss_v": ss,
                "out_v": out,
                "early_current_a": early,
                "late_current_a": late,
                "largest_observed_current_a": maximum,
                "early_cycles": early_cycles,
                "late_cycles": cycles,
                "stable": stable,
                "method": method,
            }
        )
    slope = None
    pair_slopes = None
    if all(point["late_current_a"] is not None for point in points):
        x = np.asarray(GAIN_LEVELS_V)
        y = np.asarray([point["late_current_a"] for point in points], dtype=float)
        slope = float(np.polyfit(x, y, 1)[0])
        pair_slopes = (np.diff(y) / np.diff(x)).tolist()
    return {
        "value": slope,
        "unit": "A/V",
        "points": points,
        "pair_slopes_a_per_v": pair_slopes,
        "state": {"status": "READY" if all(checks.values()) else "UNKNOWN", "checks": checks},
        "method": "three prespecified 0.80/0.85/0.90 V external-L points; no segment selection",
        "raw_points": raw.npoints,
    }


def _judge_gain(row: CitedRow, measurement: dict[str, Any]) -> dict[str, Any]:
    if measurement["state"]["status"] != "READY":
        return {"verdict": "UNKNOWN", "reason": "gain stimulus or settled response not observed"}
    slope = measurement["value"]
    if slope is None or not math.isfinite(slope) or row.typ_value is None:
        return {"verdict": "UNKNOWN", "reason": "three-point gain slope unavailable"}
    lower = (1 - GAIN_TYPICAL_FRACTION) * row.typ_value
    upper = (1 + GAIN_TYPICAL_FRACTION) * row.typ_value
    return {
        "verdict": "PASS" if lower <= slope <= upper else "FAIL",
        "basis": "cited typical gain with predeclared synthetic +/-10% guard at nominal 25 C",
        "band_a_per_v": [lower, upper],
        "reason": None,
        "low_comp_coverage": "NOT TESTED; historic low-COMP defect retained",
    }


def _measure_limit(mode: str, bench: BuckBench, raw: RawFile) -> dict[str, Any]:
    t, signals = _traces(raw)
    start, end = bench.window_s
    switch_at = float(bench.metadata["switch_at_s"])
    pre_out = _stats(t, signals["V(out)"], switch_at - 0.3e-3, switch_at - 0.05e-3)
    post_out = _stats(t, signals["V(out)"], start, end)
    midpoint = (start + end) / 2
    early_out = _stats(t, signals["V(out)"], start, midpoint)
    late_out = _stats(t, signals["V(out)"], midpoint, end)
    vin = _stats(t, signals["V(vin)"], start, end)
    en = _stats(t, signals["V(en)"], start, end)
    ss = _stats(t, signals["V(ss)"], start, end)
    sense = _stats(t, signals["V(vsense)"], start, end)
    inductor = _stats(t, signals["I(Lout)"], start, end)
    early_inductor = _stats(t, signals["I(Lout)"], start, midpoint)
    late_inductor = _stats(t, signals["I(Lout)"], midpoint, end)
    if mode == "SW":
        peak, cycles, maximum = _cycle_peaks(
            t,
            signals["V(ph)"],
            signals["I(Lout)"],
            start,
            end,
            0.5 * bench.vin_v,
        )
        method = "external Lout median complete-cycle peak"
        resolved = peak is not None and cycles >= 10
    else:
        peak = None
        cycles = None
        maximum = None
        method = "external Lout time-weighted mean; cannot infer peak limit from AVG"
        resolved = inductor["npoints"] >= 20
    checks = {
        "vin_12v": vin["min"] >= 11.9 and vin["max"] <= 12.1,
        "en_high": en["min"] > 2,
        "ss_settled": ss["min"] > 0.828,
        "resistive_overload_window_post_switch": start >= switch_at + 0.6e-3 - 1e-12,
        "pre_fault_output_formed": pre_out["mean"] > 0.5,
        "pre_fault_output_settled": (
            pre_out["max"] - pre_out["min"] <= 0.02 * abs(pre_out["mean"])
        ),
        "post_fault_output_settled": (
            abs(late_out["mean"] - early_out["mean"]) <= 0.02 * abs(post_out["mean"])
        ),
        "post_fault_current_settled": (
            abs(late_inductor["mean"] - early_inductor["mean"])
            <= max(0.05, 0.05 * abs(late_inductor["mean"]))
        ),
        "delivered_current": inductor["mean"] > 0.5,
        "resolved_waveform": resolved,
    }
    collapsed = post_out["mean"] <= (1 - LIMIT_COLLAPSE_FRACTION) * pre_out["mean"]
    return {
        "value": peak,
        "unit": "A",
        "median_cycle_peak_a": peak,
        "maximum_cycle_peak_a": maximum,
        "complete_cycles": cycles,
        "inductor_mean_a": inductor["mean"],
        "inductor_early_mean_a": early_inductor["mean"],
        "inductor_late_mean_a": late_inductor["mean"],
        "pre_fault_output_v": pre_out,
        "post_fault_output_v": post_out,
        "post_fault_early_output_v": early_out,
        "post_fault_late_output_v": late_out,
        "post_fault_output_collapse_fraction": 1 - post_out["mean"] / pre_out["mean"]
        if pre_out["mean"] > 0
        else None,
        "vin_v": vin,
        "en_v": en,
        "ss_v": ss,
        "vsense_v": sense,
        "state": {
            "status": "READY" if all(checks.values()) else "UNKNOWN",
            "checks": checks,
            "post_fault_output_collapse_observed": collapsed,
            "collapse_guard_fraction": LIMIT_COLLAPSE_FRACTION,
        },
        "method": method,
        "raw_points": raw.npoints,
    }


def _judge_limit(
    row: CitedRow, mode: str, bench: BuckBench, measurement: dict[str, Any]
) -> dict[str, Any]:
    if measurement["state"]["status"] != "READY":
        return {"verdict": "UNKNOWN", "reason": "post-soft-start overload state not observed"}
    if mode == "AVG":
        return {
            "verdict": "UNKNOWN",
            "reason": (
                "AVG external mean current cannot prove the cited peak threshold"
                if measurement["state"]["post_fault_output_collapse_observed"]
                else "post-fault output did not collapse; AVG peak threshold remains unproved"
            ),
            "scope": "measured average response only; no silicon or peak-limit claim",
        }
    peak = measurement["value"]
    if peak is None or not math.isfinite(peak) or bench.ilim_instance_a is None:
        return {"verdict": "UNKNOWN", "reason": "SW external complete-cycle peak unavailable"}
    target = bench.ilim_instance_a
    lower, upper = target - LIMIT_DYNAMIC_ALLOWANCE_A, target + LIMIT_DYNAMIC_ALLOWANCE_A
    if lower <= peak <= upper and not measurement["state"]["post_fault_output_collapse_observed"]:
        return {
            "verdict": "UNKNOWN",
            "reason": "peak is near selected ILIM, but output did not collapse under overload",
            "response_band_a": [lower, upper],
            "dynamic_allowance_a": LIMIT_DYNAMIC_ALLOWANCE_A,
        }
    return {
        "verdict": "PASS" if lower <= peak <= upper else "FAIL",
        "basis": "external SW peak response to selected template ILIM instance parameter",
        "selected_template_ilim_a": target,
        "response_band_a": [lower, upper],
        "dynamic_allowance_a": LIMIT_DYNAMIC_ALLOWANCE_A,
        "cited_static_threshold_band_a": [row.min_value, row.max_value],
        "scope": "template-parameter response at VIN=12 V, 25 C; not silicon process corners",
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
        "fault_override": (
            {"GMCS": WRONG_GMCS_A_PER_V} if case == "gain" else {"ILIM": WRONG_ILIM_A}
        )
        if variant == "fault"
        else None,
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
                raw = read_raw(run.raw_path)
                measurement = (
                    _measure_gain(mode, bench, raw)
                    if case == "gain"
                    else _measure_limit(mode, bench, raw)
                )
                judgement = (
                    _judge_gain(row, measurement)
                    if case == "gain"
                    else _judge_limit(row, mode, bench, measurement)
                )
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
    m2 = {
        bench.name: bench
        for bench in build_buck_system_benches(spec, parts, requirements_path=requirements)
    }
    if any(case not in m2 or m2[case].ports != sw.ports for case in CASES):
        raise ValueError("M2 application or physical-port contract changed")
    benches = {**{case: m2[case] for case in CASES}, "gain": _gain_bench(m2["gain"])}
    out.mkdir(parents=True, exist_ok=True)
    models = {"SW": out / "sw-model.lib", "AVG": out / "avg-model.lib"}
    sw.write(models["SW"])
    avg.write(models["AVG"])
    hashes = {mode: _sha256(model) for mode, model in models.items()}
    if hashes != REVIEWED_MODEL_SHA256 or ".param L_EXT=" not in avg.library_text:
        raise ValueError("reviewed SW/AVG model hash or external-L parameter changed")
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
        "gain_levels_v": list(GAIN_LEVELS_V),
        "gain_typical_fraction": GAIN_TYPICAL_FRACTION,
        "limit_dynamic_allowance_a": LIMIT_DYNAMIC_ALLOWANCE_A,
        "limit_collapse_guard_fraction": LIMIT_COLLAPSE_FRACTION,
        "verdict_scope": "active 3-point gain and SW template ILIM; AVG ILIM is intentionally UNKNOWN",
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
    by_key = {(row["case"], row["mode"], row["variant"]): row for row in records}
    controls = []
    for case in CASES:
        for mode in MODES:
            clean = by_key[(case, mode, "clean")]
            fault = by_key[(case, mode, "fault")]
            expected_gap = case.startswith("limit_") and mode == "AVG"
            if args.emit_only:
                status = "DECK_ONLY"
            elif expected_gap:
                status = (
                    "EXPECTED_UNKNOWN"
                    if clean["status"] == "MEASURED"
                    and clean["verdict"] == "UNKNOWN"
                    and clean.get("reason")
                    == "AVG external mean current cannot prove the cited peak threshold"
                    and clean["measurement"]["state"]["post_fault_output_collapse_observed"]
                    and fault["status"] == "MEASURED"
                    and fault["verdict"] == "UNKNOWN"
                    and fault["measurement"]["state"]["status"] == "READY"
                    else "VIOLATED"
                )
            else:
                status = (
                    "OK"
                    if clean["verdict"] == "PASS" and fault["verdict"] == "FAIL"
                    else "VIOLATED"
                )
            controls.append(
                {
                    "case": case,
                    "mode": mode,
                    "status": status,
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
        "measured": sum(row["status"] == "MEASURED" for row in records),
        "unknown_measurements": sum(row["status"] == "UNKNOWN" for row in records),
        "expected_unknown_verdicts": sum(
            row["case"].startswith("limit_")
            and row["mode"] == "AVG"
            and row["status"] == "MEASURED"
            and row["verdict"] == "UNKNOWN"
            for row in records
        ),
        "failed": sum(row["status"] == "RUN_FAILED" for row in records),
        "control_violations": sum(row["status"] == "VIOLATED" for row in controls),
    }
    _write_json(out / "manifest.json", manifest)
    return int(
        manifest["failed"] > 0
        or manifest["unknown_measurements"] > 0
        or manifest["control_violations"] > 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
