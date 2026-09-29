"""Frozen, independently sourced qualification of a compiled buck candidate.

The reviewed M4b1 slice can judge four nominal quantities. M2 contributes useful
observations, but unfinished acceptance references and M4b2 checks remain UNKNOWN
in the mandatory checklist. This report supplements the existing row harness; it
never replaces its coverage or declares the whole family qualified from four rows.
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from boardmodeler.authoring import buck_qualification as nominal
from boardmodeler.authoring.buck_system_fixtures import (
    BuckBench,
    BuckBenchParts,
    CitedRow,
    GainPoint,
    _check_verified_row,
    build_buck_system_benches,
    evaluate_waveform,
    render_deck,
)
from boardmodeler.authoring.deck_policy import check_deck, safe_deck_summary
from boardmodeler.authoring.model_syntax import validate_library
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes, sha256_file
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import read_raw

PLAN_VERSION = 1
CHECKLIST_ID = "tps54332_m2_m4b1_v1"
NOMINAL_CHECKS = ("vref", "ss_charge", "shutdown_iq", "operating_iq")
M2_CHECKS = ("gain", "limit_min", "limit_max", "ripple", "edge", "load_step", "startup")
UNFINISHED_CHECKS = ("switching_frequency", "uvlo_rise", "uvlo_fall", "en_rise", "en_fall")
MANDATORY_CHECKS = NOMINAL_CHECKS + M2_CHECKS + UNFINISHED_CHECKS
MAX_TIMEOUT_S = 120.0
MAX_RAW_BYTES = 64 * 1024 * 1024


def _canonical(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _implementation_hashes() -> tuple[tuple[str, str], ...]:
    if getattr(sys, "frozen", False):
        return (("application", sha256_file(Path(sys.executable))),)
    root = Path(__file__).resolve().parents[1]
    return tuple(
        (name, sha256_file(root / name))
        for name in (
            "authoring/qualification.py",
            "authoring/buck_qualification.py",
            "authoring/buck_system_fixtures.py",
            "authoring/deck_policy.py",
            "authoring/model_syntax.py",
            "simulation/raw.py",
            "simulation/ltspice.py",
        )
    )


def _bench(payload: str) -> BuckBench:
    data = json.loads(payload)
    data["source_rows"] = tuple(CitedRow(**row) for row in data["source_rows"])
    data["gain_points"] = tuple(
        GainPoint(
            point["comp_v"],
            tuple(point["early_window_s"]),
            tuple(point["late_window_s"]),
            point["measure_name"],
        )
        for point in data["gain_points"]
    )
    for key in ("ports", "nodes", "components", "measures", "saved_signals", "window_s"):
        data[key] = tuple(data[key])
    return BuckBench(**data)


@dataclass(frozen=True)
class QualificationCheck:
    """Immutable source/fixture snapshots, never pointers to candidate parameters."""

    check_id: str
    kind: Literal["nominal", "m2", "gap"]
    bench_json: str | None = None
    row_json: str | None = None
    reason: str | None = None
    variant: Literal["clean", "fault"] = "clean"

    def __post_init__(self) -> None:
        if self.kind not in ("nominal", "m2", "gap") or self.variant not in ("clean", "fault"):
            raise ValueError("qualification_check_kind")
        if self.kind == "gap":
            if not self.reason or self.bench_json is not None or self.row_json is not None:
                raise ValueError("qualification_gap_needs_reason")
        elif self.bench_json is None:
            raise ValueError("qualification_check_needs_frozen_bench")
        if self.kind == "nominal" and self.row_json is None:
            raise ValueError("qualification_check_needs_cited_limit")
        for text in (self.bench_json, self.row_json):
            if text is not None and _canonical(json.loads(text)) != text:
                raise ValueError("qualification_check_not_canonical")


@dataclass(frozen=True)
class QualificationPlan:
    """A frozen checklist and evidence; deliberately contains no candidate values."""

    part: str
    subckt: str
    mode: Literal["SW", "AVG"]
    spec_digest: str
    spec_json: str
    requirements_sha256: str
    requirements_json: str
    checks: tuple[QualificationCheck, ...]
    implementation_hashes: tuple[tuple[str, str], ...]
    schema_version: int = PLAN_VERSION
    checklist_id: str = CHECKLIST_ID

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_VERSION or self.checklist_id != CHECKLIST_ID:
            raise ValueError("qualification_plan_version")
        if self.mode not in ("SW", "AVG"):
            raise ValueError("qualification_plan_mode")
        frozen = SpecSet.from_json(self.spec_json)
        if (
            frozen.digest() != self.spec_digest
            or frozen.part != self.part
            or frozen.subckt != self.subckt
        ):
            raise ValueError("qualification_spec_changed")
        if sha256_bytes(self.requirements_json.encode("utf-8")) != self.requirements_sha256:
            raise ValueError("qualification_requirements_changed")
        clean = tuple(check.check_id for check in self.checks if check.variant == "clean")
        if clean != MANDATORY_CHECKS:
            raise ValueError("qualification_mandatory_checklist_changed")
        identifiers = [check.check_id for check in self.checks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("qualification_duplicate_check")
        faults = tuple(check.check_id for check in self.checks if check.variant == "fault")
        if faults and faults != tuple(f"{name}_fault" for name in NOMINAL_CHECKS):
            raise ValueError("qualification_control_checklist_changed")
        source = json.loads(self.requirements_json)
        if source.get("document", {}).get("doc_id") != frozen.doc_id:
            raise ValueError("qualification_source_document_changed")
        verified = {}
        for row in source["requirements"]:
            identifier = row["req_id"]
            if identifier in verified:
                raise ValueError("qualification_duplicate_source")
            verified[identifier] = row
        spec_rows = {row.char_id: row for row in frozen.characteristics}
        for check in self.checks:
            if check.kind == "gap":
                continue
            bench = _bench(check.bench_json or "")
            name = check.check_id.removesuffix("_fault")
            if bench.name != name or bench.part != self.part or bench.subckt != self.subckt:
                raise ValueError("qualification_bench_identity_changed")
            rows = bench.source_rows
            if check.kind == "nominal":
                row = CitedRow(**json.loads(check.row_json or ""))
                if name not in NOMINAL_CHECKS or row.char_id != nominal.ROW_IDS[name]:
                    raise ValueError("qualification_nominal_row_changed")
                rows += (row,)
            elif name not in M2_CHECKS:
                raise ValueError("qualification_M2_check_changed")
            for row in rows:
                original = spec_rows.get(row.char_id)
                requirement = verified.get(row.char_id)
                if (
                    original is None
                    or CitedRow.from_characteristic(original) != row
                    or requirement is None
                    or requirement.get("citation_verified") is not True
                    or original.req_class.upper() == "ABSOLUTE_MAXIMUM"
                ):
                    raise ValueError("qualification_limit_not_verified_source")
                _check_verified_row(row, requirement, frozen.doc_id)
        for check in self.checks:
            if check.variant == "fault":
                clean_check = next(c for c in self.checks if c.check_id == check.check_id[:-6])
                if (check.kind, check.bench_json, check.row_json) != (
                    clean_check.kind,
                    clean_check.bench_json,
                    clean_check.row_json,
                ):
                    raise ValueError("qualification_control_source_changed")

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.payload()))

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> QualificationPlan:
        data = json.loads(text)
        data["checks"] = tuple(QualificationCheck(**check) for check in data["checks"])
        data["implementation_hashes"] = tuple(tuple(item) for item in data["implementation_hashes"])
        return cls(**data)


def build_buck_qualification_plan(
    spec: SpecSet,
    requirements_path: Path,
    *,
    mode: Literal["SW", "AVG"] = "SW",
    include_controls: bool = False,
    include_m2_observations: bool = False,
) -> QualificationPlan:
    """Freeze reviewed TPS54332 fixtures before inspecting any model library.

    The 12-V, nominal-25-C fixture/state policy is reviewed for TPS54332DDA only.
    Other parts retain every mandatory check with an explicit unsupported reason.
    Fault controls are optional synthetic instance overrides, never device evidence.
    M2 observations require explicit opt-in because their acceptance references are
    unfinished; default builds run only the four nominal checks.
    """
    source = Path(requirements_path).read_bytes()
    source_text = source.decode("utf-8")
    parts = BuckBenchParts()
    benches: dict[str, BuckBench] = {}
    rows: dict[str, CitedRow] = {}
    problem = "reviewed_fixture_part_not_supported: nominal fixture is TPS54332DDA only"
    if spec.part.upper() == "TPS54332DDA":
        try:
            benches = {
                item.name: item
                for item in build_buck_system_benches(
                    spec, parts, requirements_path=requirements_path
                )
            }
            problem = ""
        except (ValueError, KeyError, TypeError) as exc:
            problem = f"reviewed_bench_unavailable: {exc}"
        try:
            rows = nominal._cited_rows(spec, requirements_path)
        except (ValueError, KeyError, TypeError) as exc:
            problem = problem or f"reviewed_nominal_rows_unavailable: {exc}"
    if Path(requirements_path).read_bytes() != source:
        raise ValueError("qualification_requirements_changed_during_freeze")
    checks: list[QualificationCheck] = []
    for name in NOMINAL_CHECKS:
        if rows and "startup" in benches:
            bench = nominal._case_bench(benches["startup"], name, rows, parts)
            checks.append(
                QualificationCheck(
                    name, "nominal", _canonical(asdict(bench)), _canonical(asdict(rows[name]))
                )
            )
        else:
            checks.append(
                QualificationCheck(name, "gap", reason=problem or "nominal_fixture_missing")
            )
    for name in M2_CHECKS:
        if mode == "AVG":
            reason = (
                "averaged_model_cannot_qualify_switching_ripple_or_edges"
                if name in ("ripple", "edge")
                else "reviewed_M2_switching_evaluator_not_qualified_for_AVG"
            )
            checks.append(QualificationCheck(name, "gap", reason=reason))
        elif not include_m2_observations:
            checks.append(
                QualificationCheck(
                    name,
                    "gap",
                    reason="M2 observations not requested; independently reviewed acceptance/reference remains open",
                )
            )
        elif name in benches:
            checks.append(
                QualificationCheck(
                    name,
                    "m2",
                    _canonical(asdict(benches[name])),
                    reason="M2 observation only: independently reviewed acceptance/reference remains open",
                )
            )
        else:
            checks.append(QualificationCheck(name, "gap", reason=problem or "M2_fixture_missing"))
    checks.extend(
        QualificationCheck(
            name, "gap", reason="reviewed independent M4b2/M4b3 check not implemented"
        )
        for name in UNFINISHED_CHECKS
    )
    if include_controls:
        checks.extend(
            QualificationCheck(
                f"{check.check_id}_fault",
                check.kind,
                check.bench_json,
                check.row_json,
                check.reason,
                "fault",
            )
            for check in tuple(checks[: len(NOMINAL_CHECKS)])
        )
    return QualificationPlan(
        spec.part,
        spec.subckt,
        mode,
        spec.digest(),
        spec.to_json(),
        sha256_bytes(source),
        source_text,
        tuple(checks),
        _implementation_hashes(),
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run_bounded(executable, deck, folder, *, timeout_s, cancel, max_raw_bytes):
    """Cancel promptly on caller stop or oversized output; never load a large raw.

    Disk output may exceed the cap during a polling interval or process shutdown.
    Such output remains evidence of an incomplete check, never a passing waveform.
    """
    stopped = threading.Event()
    run_cancel = threading.Event()
    oversized = threading.Event()

    def watch() -> None:
        while not stopped.wait(0.05):
            if cancel is not None and cancel.is_set():
                run_cancel.set()
            try:
                if sum(path.stat().st_size for path in folder.glob("*.raw")) > max_raw_bytes:
                    oversized.set()
                    run_cancel.set()
            except OSError:
                pass  # Simulator may be replacing an output while it is inspected.

    watcher = threading.Thread(target=watch, name="qualification-output-bound", daemon=True)
    watcher.start()
    try:
        observed = run_batch(
            executable,
            deck,
            folder,
            timeout_s=timeout_s,
            cancel=run_cancel,
            lock_timeout_s=timeout_s,
        )
    finally:
        stopped.set()
        watcher.join(timeout=1.0)
    if sum(path.stat().st_size for path in folder.glob("*.raw")) > max_raw_bytes:
        oversized.set()
    return observed, oversized.is_set()


def run_qualification(
    plan: QualificationPlan,
    model_library: Path,
    ltspice: Path | None,
    workdir: Path,
    *,
    design_sha256: str | None = None,
    timeout_s: float = 120.0,
    max_raw_bytes: int = MAX_RAW_BYTES,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Judge an immutable candidate copy and retain every check and raw/log artifact.

    Returns and saves ``qualification-report.json`` plus the frozen plan. No
    cached neighboring file is accepted as proof; every executable check runs.
    The report is supplemental and never rewrites the ordinary harness report.
    """
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise ValueError("qualification_timeout_must_be_within_120_seconds")
    if type(max_raw_bytes) is not int or not 0 < max_raw_bytes <= MAX_RAW_BYTES:
        raise ValueError("qualification_raw_limit_must_be_within_64_MiB")
    # Reconstruct the frozen object to validate its immutable source associations.
    plan = QualificationPlan.from_json(plan.to_json())
    root = Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "qualification-plan.json").write_text(plan.to_json(), encoding="utf-8", newline="\n")
    execution_id = uuid.uuid4().hex
    evidence_root = root / f"run-{execution_id}"
    evidence_root.mkdir()
    (evidence_root / "qualification-plan.json").write_text(
        plan.to_json(), encoding="utf-8", newline="\n"
    )
    candidate = Path(model_library).read_bytes()
    candidate_sha = sha256_bytes(candidate)
    staged = evidence_root / f"candidate-{candidate_sha[:16]}.lib"
    staged.write_bytes(candidate)
    executable = None if ltspice is None else Path(ltspice)
    simulator_sha = (
        sha256_file(executable) if executable is not None and executable.is_file() else None
    )
    integrity_problem = None
    if plan.implementation_hashes != _implementation_hashes():
        integrity_problem = "qualification_implementation_changed: freeze a new reviewed plan"
    try:
        validate_library(staged)
        library_text = candidate.decode("utf-8")
        if re.search(r"(?im)^\s*\.(?:include|inc|lib)\b", library_text):
            raise ValueError("qualification_requires_self_contained_library")
        policy = check_deck(library_text, root=root, model_path=staged)
        if not policy.ok:
            raise ValueError(safe_deck_summary(policy))
    except Exception as exc:
        integrity_problem = f"qualification_model_invalid: {type(exc).__name__}: {exc}"
    started = time.monotonic()
    records: list[dict[str, Any]] = []
    for check in plan.checks:
        record: dict[str, Any] = {
            "check_id": check.check_id,
            "variant": check.variant,
            "origin": "synthetic_fault_control"
            if check.variant == "fault"
            else "device_qualification",
            "status": "UNKNOWN",
            "reason": check.reason,
            "plan_sha256": plan.sha256,
            "model_sha256": candidate_sha,
            "artifacts": {},
        }
        records.append(record)
        if check.kind == "gap":
            continue
        if integrity_problem:
            record["reason"] = integrity_problem
            continue
        if simulator_sha is None:
            record.update(status="BLOCKED", reason="ltspice_unavailable")
            continue
        if cancel is not None and cancel.is_set():
            record["reason"] = "cancelled: qualification check not run"
            continue
        folder = evidence_root / check.check_id
        folder.mkdir(parents=True, exist_ok=True)
        tick = time.monotonic()
        try:
            if (
                sha256_file(staged) != candidate_sha
                or Path(model_library).read_bytes() != candidate
            ):
                raise ValueError("qualification_candidate_changed")
            bench = _bench(check.bench_json or "")
            if check.kind == "nominal":
                deck_text, _ = nominal._deck(
                    bench, staged, plan.mode, check.variant, BuckBenchParts().inductance_h
                )
            else:
                deck_text = render_deck(bench, staged)
            policy = check_deck(deck_text, root=root, model_path=staged)
            if not policy.ok:
                raise ValueError(safe_deck_summary(policy))
            deck = folder / "deck.cir"
            deck.write_text(deck_text, encoding="utf-8", newline="\n")
            record["artifacts"]["deck"] = {"path": str(deck), "sha256": sha256_file(deck)}
            observed, oversized = _run_bounded(
                executable,
                deck,
                folder,
                timeout_s=timeout_s,
                cancel=cancel,
                max_raw_bytes=max_raw_bytes,
            )
            record["simulator_observed"] = observed.observed()
            record["simulation_seconds"] = observed.wall_s
            for name, path in (("raw", observed.raw_path), ("log", observed.log_path)):
                if path is not None and path.is_file():
                    record["artifacts"][name] = {
                        "path": str(path),
                        "sha256": sha256_file(path),
                        "bytes": path.stat().st_size,
                    }
            if oversized:
                record["reason"] = "waveform_output_limit_exceeded: output retained, not measured"
                continue
            if (
                sha256_file(staged) != candidate_sha
                or Path(model_library).read_bytes() != candidate
                or sha256_file(deck) != record["artifacts"]["deck"]["sha256"]
            ):
                raise ValueError("qualification_candidate_or_deck_changed_during_run")
            if not observed.ok or not {"raw", "log"}.issubset(record["artifacts"]):
                record["reason"] = "simulation_incomplete: completed raw and log are required"
                continue
            raw = read_raw(observed.raw_path)
            if check.kind == "nominal":
                row = CitedRow(**json.loads(check.row_json or ""))
                measurement = nominal._measure(bench.name, plan.mode, bench, row, raw)
                canonical_json_bytes(measurement)  # Nonfinite data cannot enter an evidence record.
                judgement = nominal._judge(row, measurement)
                record.update(
                    status=judgement["verdict"],
                    reason=judgement["reason"],
                    measurement=measurement,
                    judgement=judgement,
                )
            else:
                measurement = evaluate_waveform(bench, raw)
                canonical_json_bytes(asdict(measurement))
                record["measurement"] = asdict(measurement)
                # Measurable does not imply that a reviewed acceptance reference exists.
                record["reason"] = measurement.reason or check.reason
        except Exception as exc:
            record.update(
                status="UNKNOWN", reason=f"qualification_unavailable: {type(exc).__name__}: {exc}"
            )
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - tick, 6)
            _write_json(folder / "result.json", record)
    clean = [record for record in records if record["variant"] == "clean"]
    controls = [record for record in records if record["variant"] == "fault"]
    status = (
        "FAIL"
        if any(record["status"] == "FAIL" for record in clean)
        else "BLOCKED"
        if any(record["status"] == "BLOCKED" for record in clean)
        else "PASS"
        if all(record["status"] == "PASS" for record in clean)
        else "UNKNOWN"
    )
    report = {
        "schema_version": PLAN_VERSION,
        "record_kind": "qualification_report",
        "execution_id": execution_id,
        "evidence_directory": str(evidence_root),
        "checklist_id": plan.checklist_id,
        "part": plan.part,
        "mode": plan.mode,
        "status": status,
        "family_qualified": status == "PASS",
        "scope": "reviewed TPS54332 nominal 12-V / 25-C M4b1 checks; M2 observations and open M4b2/M4b3 checks retained",
        "plan_sha256": plan.sha256,
        "spec_digest": plan.spec_digest,
        "requirements_sha256": plan.requirements_sha256,
        "design_sha256": design_sha256,
        "model_sha256": candidate_sha,
        "candidate_path": str(staged),
        "simulator_sha256": simulator_sha,
        "implementation_hashes": dict(plan.implementation_hashes),
        "mandatory_check_ids": list(MANDATORY_CHECKS),
        "results": records,
        "counts": {
            state: sum(record["status"] == state for record in clean)
            for state in ("PASS", "FAIL", "UNKNOWN", "BLOCKED")
        },
        "controls": {
            "requested": bool(controls),
            "all_faults_rejected": bool(controls)
            and all(record["status"] == "FAIL" for record in controls),
            "all_pairs_discriminate": bool(controls)
            and all(
                record["status"] == "FAIL"
                and any(
                    c["check_id"] == record["check_id"][:-6] and c["status"] == "PASS"
                    for c in clean
                )
                for record in controls
            ),
            "note": "Synthetic controls are excluded from the device verdict count; a control UNKNOWN is not a successful rejection.",
        },
        "bounds": {"per_check_timeout_seconds": timeout_s, "raw_bytes": max_raw_bytes},
        "simulation_seconds": sum(record.get("simulation_seconds", 0.0) for record in records),
        "total_seconds": round(time.monotonic() - started, 6),
    }
    _write_json(evidence_root / "qualification-report.json", report)
    _write_json(root / "qualification-report.json", report)
    return report
