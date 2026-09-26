r"""Diagnose M4a AVG startup input-power spikes without changing the model.

Run one copied, previously measured startup deck in a new ignored runs/ folder.
The only electrical change is a zero-volt shunt between the top-level input
capacitor and XU1 VIN. LTspice is used only at the explicitly supplied path.

PowerShell example::

    .\.venv\Scripts\python.exe tools\tps54332_m4a_power_probe.py --accepted-dir runs\m4a-smoke-20260926-2 --ltspice-exe "C:\path\to\LTspice.exe" --out runs\m4a-power-probe-1

The raw waveform is always retained. MEASURED means observations exist, not
that the averaged model passed an energy or physics requirement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import read_raw

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
SAVED = (
    "V(vin)",
    "V(vin_dut)",
    "I(Vin)",
    "I(Cin)",
    "I(Vsense)",
    "I(Lout)",
    "V(out)",
    "V(ph)",
    "I(Dcatch)",
    "V(comp)",
    "V(ss)",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _source(accepted_dir: Path) -> tuple[str, dict[str, Any], tuple[float, float], float]:
    source_deck = accepted_dir / "startup-avg" / "startup-avg.cir"
    source_record = accepted_dir / "startup-avg" / "result.json"
    source_model = accepted_dir / "avg-model.lib"
    accepted = json.loads(source_record.read_text(encoding="utf-8"))
    if (
        accepted.get("status") != "MEASURED"
        or accepted.get("case") != "startup"
        or accepted.get("mode") != "AVG"
    ):
        raise ValueError("accepted run must contain a measured M4a startup AVG record")
    if _sha256(source_deck) != accepted.get("deck_sha256"):
        raise ValueError("accepted startup deck hash changed")
    if _sha256(source_model) != accepted.get("model_sha256"):
        raise ValueError("accepted AVG model hash changed")
    window = accepted["measurements"]["final_window_s"]
    if len(window) != 2 or not all(math.isfinite(float(t)) for t in window):
        raise ValueError("accepted final window is invalid")
    lines = source_deck.read_text(encoding="utf-8").splitlines()
    loads = [line.split() for line in lines if line.lower().startswith("rload ")]
    if len(loads) != 1 or loads[0][1:3] != ["out", "0"]:
        raise ValueError("expected one output load resistor")
    load_ohm = float(loads[0][3])
    if load_ohm <= 0 or not math.isfinite(load_ohm):
        raise ValueError("invalid output load")
    return "\n".join(lines) + "\n", accepted, (float(window[0]), float(window[1])), load_ohm


def _diagnostic_deck(source: str, model: Path, source_model: Path) -> str:
    lines = source.splitlines()
    includes = [i for i, line in enumerate(lines) if line.lower().startswith(".include ")]
    saves = [i for i, line in enumerate(lines) if line.lower().startswith(".save ")]
    instances = [i for i, line in enumerate(lines) if line.startswith("XU1 ")]
    if len(includes) != 1 or len(saves) != 1 or len(instances) != 1:
        raise ValueError("expected one include, save directive and XU1 instance")
    included = re.fullmatch(r'\.include\s+"([^"]+)"', lines[includes[0]], flags=re.I)
    if included is None or Path(included[1]).resolve() != source_model.resolve():
        raise ValueError("accepted deck includes an unexpected model")
    parts = lines[instances[0]].split()
    if len(parts) != 12 or parts[2].lower() != "vin" or parts[10] != "TPS54332DDA":
        raise ValueError("accepted XU1 pin order changed")
    parts[2] = "vin_dut"
    lines[instances[0]] = " ".join(parts)
    lines.insert(instances[0], "Vsense vin vin_dut 0")
    lines[includes[0]] = f'.include "{model.resolve().as_posix()}"'
    # The inserted line precedes .save in the accepted deck.
    lines[saves[0] + 1] = ".save " + " ".join(SAVED)
    lines.insert(1, "* Diagnostic bench change: 0-V Vsense isolates XU1 VIN from top-level Cin.")
    return "\n".join(lines) + "\n"


def _window(t: np.ndarray, y: np.ndarray, start: float, end: float) -> dict[str, float | int]:
    if len(t) != len(y) or len(t) < 3 or np.any(np.diff(t) < 0):
        raise ValueError("raw time axis or signal is invalid")
    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)):
        raise ValueError("raw waveform is nonfinite")
    if start < t[0] or end > t[-1] or start >= end:
        raise ValueError("raw waveform does not cover accepted final window")
    raw = y[(t >= start) & (t <= end)]
    if len(raw) < 2:
        raise ValueError("too few raw points in accepted final window")
    # Last sample at duplicate event times matches the M4a reader's convention.
    keep = np.r_[np.diff(t) > 0, True]
    tt, yy = t[keep], y[keep]
    inside = (tt > start) & (tt < end)
    x = np.r_[start, tt[inside], end]
    values = np.r_[np.interp(start, tt, yy), yy[inside], np.interp(end, tt, yy)]
    area = float(np.trapezoid(values, x))
    positive = 0.0
    negative = 0.0
    for left, right, span in zip(values[:-1], values[1:], np.diff(x), strict=True):
        if left >= 0 and right >= 0:
            positive += float(0.5 * (left + right) * span)
        elif left <= 0 and right <= 0:
            negative += float(0.5 * (left + right) * span)
        else:
            fraction = -left / (right - left)
            if left > 0:
                positive += float(0.5 * left * span * fraction)
                negative += float(0.5 * right * span * (1 - fraction))
            else:
                negative += float(0.5 * left * span * fraction)
                positive += float(0.5 * right * span * (1 - fraction))
    return {
        "raw_min": float(np.min(raw)),
        "raw_max": float(np.max(raw)),
        "raw_samples": len(raw),
        "time_weighted_mean": area / (end - start),
        "signed_integral": area,
        "piecewise_linear_positive_integral": positive,
        "piecewise_linear_negative_integral": negative,
    }


def _spikes(
    t: np.ndarray, power: np.ndarray, start: float, end: float, threshold: float
) -> dict[str, Any]:
    keep = np.r_[np.diff(t) > 0, True]
    tt, pp = t[keep], power[keep]
    inside = (tt >= start) & (tt <= end)
    tt, pp = tt[inside], pp[inside]
    result: dict[str, Any] = {"threshold_w": threshold}
    for name, hit in (("positive", pp > threshold), ("negative", pp < -threshold)):
        starts = np.flatnonzero(hit & ~np.r_[False, hit[:-1]])
        result[name] = {
            "samples": int(np.count_nonzero(hit)),
            "episodes": len(starts),
            "first_episode_times_s": [float(tt[i]) for i in starts[:20]],
            "first_hit_s": float(tt[np.flatnonzero(hit)[0]]) if np.any(hit) else None,
            "last_hit_s": float(tt[np.flatnonzero(hit)[-1]]) if np.any(hit) else None,
        }
    return result


def _measure(
    raw_path: Path, window: tuple[float, float], load_ohm: float, threshold_w: float
) -> dict[str, Any]:
    raw = read_raw(raw_path)
    t = np.asarray(raw.time_column(), dtype=float)
    values = {name: np.asarray(raw.column(name), dtype=float) for name in SAVED}
    if any(len(column) != len(t) for column in values.values()):
        raise ValueError("a saved signal has a different number of raw points")
    vin = values["V(vin)"]
    vdut = values["V(vin_dut)"]
    ivin = values["I(Vin)"]
    icin = values["I(Cin)"]
    ishunt = values["I(Vsense)"]
    iout = values["I(Lout)"]
    vout = values["V(out)"]
    signals = {
        **values,
        "P_source_w": -vin * ivin,
        "P_cap_w": vin * icin,
        "P_dut_pin_w": vdut * ishunt,
        "P_shunt_w": (vin - vdut) * ishunt,
        "P_load_w": vout * vout / load_ohm,
        "P_ph_to_inductor_w": values["V(ph)"] * iout,
        "I_vin_kcl_residual_a": ivin + icin + ishunt,
    }
    start, end = window
    return {
        "raw_points": int(raw.npoints),
        "raw_variables": raw.variables,
        "final_window_s": [start, end],
        "sign_convention": (
            "I(Vsense)>0 enters DUT; I(Cin)>0 charges Cin; "
            "P_source=-V(vin)*I(Vin)>0 means the source delivers power"
        ),
        "final": {name: _window(t, signal, start, end) for name, signal in signals.items()},
        "source_power_spikes": _spikes(t, signals["P_source_w"], start, end, threshold_w),
        "dut_power_spikes": _spikes(t, signals["P_dut_pin_w"], start, end, threshold_w),
        "verdict": "UNJUDGED; numerical and physical interpretation requires waveform review",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-dir", type=Path, required=True)
    parser.add_argument("--ltspice-exe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--spike-threshold-w", type=float, default=50.0)
    args = parser.parse_args()
    exe = args.ltspice_exe.resolve(strict=True)
    accepted_dir = args.accepted_dir.resolve(strict=True)
    out = args.out.resolve()
    runs = RUNS.resolve(strict=True)
    if not exe.is_file() or not accepted_dir.is_dir() or not accepted_dir.is_relative_to(runs):
        raise ValueError(
            "explicit LTspice file and accepted run under this repository's runs/ are required"
        )
    if not out.is_relative_to(runs) or out.is_relative_to(accepted_dir):
        raise ValueError("fresh --out must be under runs/ and separate from --accepted-dir")
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be empty")
    if len(str(out)) > 125:
        raise ValueError("choose a shorter output path for LTspice on Windows")
    if not all(math.isfinite(x) and x > 0 for x in (args.timeout_s, args.spike_threshold_w)):
        raise ValueError("timeout and spike threshold must be finite and positive")
    source, accepted, window, load_ohm = _source(accepted_dir)
    out.mkdir(parents=True, exist_ok=True)
    source_model = accepted_dir / "avg-model.lib"
    model = out / "accepted-avg-model.lib"
    model.write_bytes(source_model.read_bytes())
    deck = out / "power-probe.cir"
    deck.write_text(_diagnostic_deck(source, model, source_model), encoding="utf-8", newline="\n")
    result: dict[str, Any] = {
        "schema_version": 1,
        "recorded_utc": datetime.now(UTC).isoformat(),
        "status": "RUN_FAILED",
        "verdict": "UNJUDGED",
        "accepted_dir": str(accepted_dir),
        "accepted_deck_sha256": accepted["deck_sha256"],
        "accepted_model_sha256": accepted["model_sha256"],
        "diagnostic_deck_sha256": _sha256(deck),
        "diagnostic_model_sha256": _sha256(model),
        "script_sha256": _sha256(Path(__file__)),
        "ltspice_exe": str(exe),
        "ltspice_exe_sha256": _sha256(exe),
        "deck_change": "0-V Vsense after top-level Cin; additional .save signals only",
        "internal_probes": "OMITTED_UNVERIFIED",
        "accepted_final_window_s": list(window),
        "load_ohm": load_ohm,
    }
    try:
        run = run_batch(exe, deck, out, timeout_s=args.timeout_s, lock_timeout_s=30.0)
        result.update(
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            ltspice_wall_s=run.wall_s,
            simulator_observed=run.observed(),
            raw_retained=bool(run.raw_path and run.raw_path.is_file()),
            raw_sha256=_sha256(run.raw_path) if run.raw_path else None,
            log_sha256=_sha256(run.log_path) if run.log_path else None,
        )
        if run.ok and run.raw_path is not None:
            result["measurements"] = _measure(
                run.raw_path, window, load_ohm, args.spike_threshold_w
            )
            result["status"] = "MEASURED"
        else:
            result["reason"] = "LTspice did not finish with readable raw and log artifacts"
    except Exception as exc:
        result["status"] = "UNKNOWN"
        result["reason"] = f"{type(exc).__name__}: {exc}"
    _write_json(out / "result.json", result)
    print(json.dumps({"status": result["status"], "out": str(out), "reason": result.get("reason")}))
    return 0 if result["status"] == "MEASURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
