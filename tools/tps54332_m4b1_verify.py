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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boardmodeler.authoring.buck_qualification import (
    FAULT_OVERRIDES,
    _case_bench,
    _cited_rows,
    _deck,
    _judge,
    _measure,
)
from boardmodeler.authoring.buck_qualification import (
    _stable_iq_draw as _stable_iq_draw,
)
from boardmodeler.authoring.buck_qualification import (
    _up_crossing as _up_crossing,
)
from boardmodeler.authoring.buck_system_fixtures import (
    BuckBench,
    BuckBenchParts,
    CitedRow,
    build_buck_system_benches,
)
from boardmodeler.authoring.qualification import _implementation_hashes
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import read_raw

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
        "implementation_hashes": provenance["implementation_hashes"],
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


def _pin_environment() -> None:
    """Pin this run offline and serial. Only the script does this, never an import:
    the test module imports this file, and a pin set at import would switch the
    network off for every other test in the same session."""
    os.environ["BOARDMODELER_NO_NETWORK"] = "1"
    os.environ["HARNESS_WORKERS"] = "1"


def main() -> int:
    _pin_environment()
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
        "implementation_hashes": dict(_implementation_hashes()),
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
