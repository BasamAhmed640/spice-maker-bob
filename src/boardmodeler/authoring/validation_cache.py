"""Reuse observed simulator evidence only when every validation input still matches."""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes, sha256_file

CACHE_VERSION = 1

#: Reports this process itself observed from the harness, per ``(cache root, key)``. The
#: report, ``.raw`` and ``.log`` on disk are writable by the authoring agent, so a cache
#: entry is reused only when this process produced it; a fresh process re-simulates the
#: existing candidate instead of trusting files nobody can bind to their run.
_OBSERVED: dict[tuple[str, str], object] = {}
_FEEDBACK: dict[tuple[str, str], object] = {}


def read_feedback(root: Path, key: str | None, spec, model: Path):
    """Return this process's measured failures, including UNKNOWN, for repair only.

    This is never a shortcut to PASS or a reusable simulation verdict.
    """
    report = _FEEDBACK.get((str(Path(root).resolve()), key)) if key else None
    if report is None or not model.is_file():
        return None
    if report.spec_digest != spec.digest() or report.model_sha256 != sha256_file(model):
        return None
    return report


@lru_cache(maxsize=64)
def _file_digest(path: str, size: int, mtime_ns: int) -> str:
    return sha256_file(Path(path))


def digest_file(path: Path) -> str:
    stat = path.stat()
    return _file_digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def validation_key(model: Path, spec, simulator: Path, timeout_s: float) -> str | None:
    if not model.is_file() or not simulator.is_file():
        return None
    # External includes would require a transitive dependency graph. Until supported,
    # refuse reuse rather than claim the top-level library hash covers those bytes. An
    # undecodable file is refused the same way: nothing can prove its bytes are covered.
    try:
        model_text = model.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    if re.search(r"(?im)^\s*\.(?:include|inc|lib)\b", model_text):
        return None
    package = Path(__file__).resolve().parents[1]
    if getattr(sys, "frozen", False):
        engine = {"application": digest_file(Path(sys.executable))}
    else:
        engine = {
            str(path.relative_to(package)): digest_file(path)
            for folder in ("authoring", "simulation", "domain")
            for path in sorted((package / folder).glob("*.py"))
        }
    return sha256_bytes(
        canonical_json_bytes(
            {
                "version": CACHE_VERSION,
                "model": sha256_file(model),
                "spec": spec.digest(),
                "simulator": digest_file(simulator),
                "timeout_s": timeout_s,
                "engine": engine,
                "python": sys.version,
                "numpy": version("numpy"),
            }
        )
    )


def read_report(root: Path, key: str | None, spec, model: Path):
    from boardmodeler.authoring.harness import HarnessReport, _judge
    from boardmodeler.authoring.probes import PROBES, judge_value
    from boardmodeler.domain.enums import Status
    from boardmodeler.simulation.deck import TranSpec
    from boardmodeler.simulation.log import parse_log
    from boardmodeler.simulation.measures import diagnose
    from boardmodeler.simulation.raw import read_raw

    if key is None:
        return None
    observed = _OBSERVED.get((str(Path(root).resolve()), key))
    if observed is None:
        return None
    directory = root / key
    try:
        report = HarnessReport.from_json((directory / "report.json").read_text(encoding="utf-8"))
        # The on-disk report must be the one this process observed, byte for byte in
        # meaning: an entry written by anyone else (including the authoring agent) is not
        # evidence, however well its neighboring checksums agree.
        if report != observed:
            return None
        if report.spec_digest != spec.digest() or report.model_sha256 != sha256_file(model):
            return None
        covered = {char.char_id for char in spec.covered()}
        if {char for row in report.outcomes for char in row.char_ids} != covered:
            return None
        cases = {
            tuple(char.char_id for char in chars): (probe_id, chars)
            for _, probe_id, chars in spec.cases()
        }
        if len(report.outcomes) != len(cases):
            return None
        seen = set()
        for row in report.outcomes:
            if row.status not in ("PASS", "FAIL") or not row.artifacts:
                return None
            case = cases.get(row.char_ids)
            if case is None or row.char_ids in seen or case[0] != row.probe_id:
                return None
            seen.add(row.char_ids)
            for filename, digest in row.artifacts.items():
                path = Path(filename).resolve()
                if not path.is_relative_to(directory.resolve()) or sha256_file(path) != digest:
                    return None
            # A cached status is never its own evidence: remeasure the hashed raw
            # waveform and compare with the frozen limits, without launching LTspice.
            probe = PROBES[row.probe_id]
            if row.probe_id == "circuit_measurement":
                from boardmodeler.authoring.circuit_probe import make_probe

                probe = make_probe(case[1][0].probe_recipe)
            params = probe.merged_params(case[1][0].probe_params)
            operating_point = params
            if row.probe_id == "circuit_measurement":
                recipe = case[1][0].probe_recipe
                operating_point = {
                    **recipe["operating_point"],
                    "temperature_C": recipe["temperature"],
                    "tstop_s": recipe["stop"],
                    "tmax_s": recipe["step"],
                }
            if row.operating_point != operating_point:
                return None
            raw_path = Path(row.run_dir) / "deck.raw"
            log_path = Path(row.run_dir) / "deck.log"
            if any(str(path.resolve()) not in row.artifacts for path in (raw_path, log_path)):
                return None
            diagnosis = diagnose(
                log=parse_log(log_path),
                raw=read_raw(raw_path),
                tran=TranSpec(
                    tstep=0,
                    tstop=operating_point["tstop_s"],
                    tstart=0,
                    tmax=operating_point["tmax_s"],
                )
                if probe.analysis == "tran"
                else None,
            )
            if diagnosis.blocked_reason():
                return None
            measured = probe.measure(raw_path, case[1][0].probe_params)
            if measured != row.measured:
                return None
            measured_key, value = judge_value(row.probe_id, measured)
            verdicts = [_judge(char, measured_key, value)[0] for char in case[1]]
            status = Status.FAIL.value if Status.FAIL.value in verdicts else Status.PASS.value
            if Status.UNKNOWN.value in verdicts or status != row.status:
                return None
        return report if report.outcomes else None
    except OSError, ValueError, KeyError, TypeError, RuntimeError:
        return None


def write_report(root: Path, key: str | None, report) -> None:
    if key is not None and report.outcomes:
        _FEEDBACK[(str(Path(root).resolve()), key)] = report
    if (
        key is None
        or not report.outcomes
        or any(row.status not in ("PASS", "FAIL") or not row.artifacts for row in report.outcomes)
    ):
        return
    directory = root / key
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "report.json.tmp"
    temporary.write_text(report.to_json(), encoding="utf-8")
    temporary.replace(directory / "report.json")
    _OBSERVED[(str(Path(root).resolve()), key)] = report


def progress_score(report, spec) -> tuple[int, int, float]:
    """Minimize unknown rows, then failed rows, then normalized electrical error."""
    from boardmodeler.authoring.probes import PROBES

    unknown = failed = 0
    error = 0.0
    for outcome in report.outcomes:
        unknown += len(outcome.char_ids) if outcome.status == "UNKNOWN" else 0
        failed += len(outcome.char_ids) if outcome.status == "FAIL" else 0
        probe = PROBES.get(outcome.probe_id)
        value = outcome.measured.get(probe.judge_key) if probe else None
        if not isinstance(value, (int, float)):
            continue
        for char_id in outcome.char_ids:
            char = spec.by_id(char_id)
            lo, hi = char.min_value, char.max_value
            if lo is None and hi is None and char.typ_value is not None:
                band = abs(char.typ_value) * 0.1
                lo, hi = char.typ_value - band, char.typ_value + band
            scale = max(abs(lo or 0), abs(hi or 0), 1e-12)
            error += (
                max((lo - value) if lo is not None else 0, (value - hi) if hi is not None else 0, 0)
                / scale
            )
    return unknown, failed, round(error, 12)
