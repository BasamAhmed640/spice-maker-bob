"""Measurements and run diagnostics (Phase 1 step 2).

``.meas`` results, log classification and truncation detection live here so that
every caller sees the same picture of a run. The rule that matters: a run that
did not reach the end of its analysis window is reported as truncated, and a
truncated run can never satisfy a "must not occur" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from boardmodeler.simulation.deck import TranSpec
from boardmodeler.simulation.log import LogSummary, Measurement
from boardmodeler.simulation.raw import RawFile

__all__ = [
    "Observation",
    "RunDiagnostics",
    "diagnose",
    "observations_from_log",
    "reached_end",
]

#: A run is considered to have reached its stop time within this relative slack
#: (the last solver step rarely lands exactly on ``tstop``).
_REACH_SLACK = 1e-3


@dataclass(frozen=True)
class Observation:
    """One measured quantity, with where it came from."""

    name: str
    value: float | None
    at_s: float | None = None
    target: str | None = None
    ok: bool = True
    source: Literal["meas", "raw"] = "meas"
    detail: str = ""

    def as_measured(self) -> float | str:
        if self.value is None:
            return self.detail or "not measured"
        return float(self.value)


def _observation_from_measurement(meas: Measurement) -> Observation:
    return Observation(
        name=meas.name,
        value=meas.value,
        at_s=meas.at_s,
        target=meas.target,
        ok=meas.ok,
        source="meas",
        detail="" if meas.ok else f".meas reported failure: {meas.text}",
    )


def observations_from_log(summary: LogSummary) -> dict[str, Observation]:
    """Convert ``.meas`` results into observations (failures preserved)."""
    return {
        name: _observation_from_measurement(meas) for name, meas in summary.measurements.items()
    }


@dataclass
class RunDiagnostics:
    """The honest state of one simulation run."""

    completed: bool = False
    truncated: bool = False
    reached_s: float | None = None
    expected_stop_s: float | None = None
    convergence_issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observations: dict[str, Observation] = field(default_factory=dict)
    raw_missing_reason: str | None = None

    @property
    def failed_measurements(self) -> list[Observation]:
        return [obs for obs in self.observations.values() if not obs.ok]

    def blocked_reason(self) -> str | None:
        """Why this run cannot produce a verdict, or ``None`` when it can.

        Convergence failure and a missing/unreadable ``.raw`` are BLOCKED: the
        simulator did not deliver data, which is not evidence about the design.
        """
        if self.convergence_issues:
            return "sim_convergence_failure: " + " | ".join(
                (self.errors + self.convergence_issues)[:2]
            )
        if self.raw_missing_reason:
            return f"sim_output_unreadable: {self.raw_missing_reason}"
        if not self.completed:
            first = self.errors[0] if self.errors else "(no error text in the log)"
            return f"sim_run_incomplete: {first}"
        return None

    def summary(self) -> str:
        bits = [
            f"completed={self.completed}",
            f"truncated={self.truncated}",
            f"reached={self.reached_s}",
        ]
        if self.expected_stop_s is not None:
            bits.append(f"expected_stop={self.expected_stop_s}")
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        if self.convergence_issues:
            bits.append(f"convergence={len(self.convergence_issues)}")
        if self.observations:
            bits.append(f"observations={len(self.observations)}")
        return "; ".join(bits)


def reached_end(
    reached_s: float | None, expected_stop_s: float | None, *, slack: float = _REACH_SLACK
) -> bool:
    """Did the saved waveform reach the declared stop time?"""
    if reached_s is None or expected_stop_s is None:
        return False
    if expected_stop_s <= 0:
        return reached_s >= expected_stop_s
    return reached_s >= expected_stop_s * (1.0 - slack)


def diagnose(
    *,
    log: LogSummary,
    raw: RawFile | None,
    tran: TranSpec | None = None,
    raw_error: str | None = None,
) -> RunDiagnostics:
    """Combine the log and the waveform into one honest picture of the run.

    ``raw_error`` carries the reason a ``.raw`` could not be read (a format
    failure), so an unreadable output is BLOCKED rather than UNKNOWN.
    """
    diag = RunDiagnostics(
        completed=log.completed,
        convergence_issues=list(log.convergence_issues),
        errors=list(log.errors),
        warnings=list(log.warnings),
        observations=observations_from_log(log),
        raw_missing_reason=raw_error,
    )
    if raw is not None:
        time_axis = raw.time_column()
        if time_axis is not None and time_axis.size:
            diag.reached_s = float(time_axis[-1])
    diag.expected_stop_s = float(tran.tstop) if tran is not None else None
    if diag.expected_stop_s is not None and diag.reached_s is not None:
        diag.truncated = not reached_end(diag.reached_s, diag.expected_stop_s)
    elif diag.completed and raw is not None and diag.reached_s is None:
        # Completed but no time axis: true for operating-point plots, which is
        # not truncation. Leave truncated=False and let the window check decide.
        diag.truncated = False
    return diag
