"""LTspice ``.log`` parsing.

The log is the authoritative record of a batch run: it says whether the run
completed, what ``.meas`` produced, and what went wrong. Rules enforced here:

* A run that did not print ``Total elapsed time`` did not complete.
* ``.meas`` failures are recorded as failures, never as a missing key that a
  caller might mistake for "nothing to check".
* Log text is never synthesized: if a pattern is not present, the corresponding
  field stays ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["LogSummary", "Measurement", "parse_log", "read_log_text"]

ELAPSED_RE = re.compile(r"Total elapsed time:\s*(?P<secs>[0-9.]+)\s*seconds", re.IGNORECASE)
# e.g. "vout_at_1ms: V(out) =0.632119259594 at 0.001"
_MEAS_OK_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.\-]*)\s*[:=]\s*(?P<target>[^=]{0,80}?)=\s*"
    r"(?P<value>[-+]?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*(?P<tail>.*)$"
)
_MEAS_AT_RE = re.compile(r"\bat\s+(?P<at>[-+]?[0-9.]+(?:[eE][-+]?[0-9]+)?)")
_MEAS_FAILED_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.\-]*)\s*[:=]?\s*(?P<target>.*?)\bFAILED\b", re.IGNORECASE
)
_TARGET_AT_RE = re.compile(r"^\((?P<at>[-+]?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*,")

_ERROR_MARKERS = (
    "error",
    "fatal",
    "unknown subcircuit",
    "this sub-circuit name is not defined",
    "missing model",
    "can't find",
    "cannot find",
    "syntax error",
    # LTspice reports an over-defined matrix without any prefix of its own:
    # "Voltage source V_en and voltage source B_enable are paralleled making an
    # over-defined circuit matrix. / You will need to correct the circuit …".
    "over-defined circuit matrix",
    "will need to correct the circuit",
)
_WARNING_MARKERS = ("warning",)
_CONVERGENCE_MARKERS = (
    "timestep too small",
    "time step too small",
    "singular matrix",
    "iteration limit reached",
    "gmin stepping failed",
    "source stepping failed",
    "convergence failed",
    "analysis failed",
)
#: The operating-point search messages that a later success supersedes.
_STEPPING_FAILURES = ("gmin stepping failed", "source stepping failed")
_OP_FOUND = "succeeded in finding the operating point"
# LTspice prefixes errors with the offending file and line: "path(2): message"
_FILELINE_RE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+)\):\s*(?P<message>.+)$")


@dataclass(frozen=True)
class Measurement:
    """One ``.meas`` result from the log."""

    name: str
    value: float | None
    target: str | None
    at_s: float | None
    failed: bool
    text: str

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.failed


@dataclass
class LogSummary:
    """Everything BoardModeler reads out of a run log."""

    path: Path | None
    text: str = ""
    completed: bool = False
    elapsed_s: float | None = None
    measurements: dict[str, Measurement] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    convergence_issues: list[str] = field(default_factory=list)
    loaded_files: list[str] = field(default_factory=list)

    @property
    def failed_measurements(self) -> list[Measurement]:
        return [m for m in self.measurements.values() if m.failed or m.value is None]

    def value(self, name: str) -> float | None:
        """Measured value by name (case-insensitive), or ``None`` if absent."""
        if name in self.measurements:
            return self.measurements[name].value
        lowered = name.lower()
        for key, meas in self.measurements.items():
            if key.lower() == lowered:
                return meas.value
        return None

    def summary_line(self) -> str:
        """One-line description used in details and reports."""
        bits = [
            f"completed={self.completed}",
            f"elapsed={self.elapsed_s if self.elapsed_s is not None else 'unknown'}s",
            f"measurements={len(self.measurements)}",
        ]
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        if self.convergence_issues:
            bits.append(f"convergence_issues={len(self.convergence_issues)}")
        if self.warnings:
            bits.append(f"warnings={len(self.warnings)}")
        return "; ".join(bits)


def read_log_text(path: Path) -> str:
    """Read a log file, tolerating either ANSI or UTF-16 content."""
    data = path.read_bytes()
    if len(data) >= 2 and data[1] == 0:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def parse_log(path: Path | None = None, *, text: str | None = None) -> LogSummary:
    """Parse log text (from ``path`` or a ``text`` override, used by tests)."""
    if text is None:
        if path is None:
            raise ValueError("parse_log requires either path or text")
        if not path.exists():
            return LogSummary(path=path)
        text = read_log_text(path)

    summary = LogSummary(path=path, text=text)
    in_loaded_files = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if in_loaded_files:
            if stripped.endswith(":") and stripped.lower().startswith("files"):
                continue
            if _looks_like_path(stripped):
                summary.loaded_files.append(stripped)
                continue
            in_loaded_files = False
        if stripped.lower().startswith("files loaded"):
            in_loaded_files = True
            continue

        elapsed = ELAPSED_RE.search(line)
        if elapsed:
            summary.elapsed_s = float(elapsed.group("secs"))
            summary.completed = True
            continue

        lowered = stripped.lower()

        if any(marker in lowered for marker in _CONVERGENCE_MARKERS):
            summary.convergence_issues.append(stripped)
            continue

        if _is_error_line(stripped, lowered):
            summary.errors.append(stripped)
            continue

        if any(marker in lowered for marker in _WARNING_MARKERS):
            summary.warnings.append(stripped)
            continue

        meas = _parse_measurement(stripped)
        if meas is not None:
            summary.measurements[meas.name] = meas

    if _OP_FOUND in text.lower():
        # LTspice tries direct Newton, then Gmin stepping, then source stepping; a failed
        # attempt followed by "... succeeded in finding the operating point" is an operating
        # point found, not a convergence failure. The failed attempts stay visible as
        # warnings; anything else (time step too small, singular matrix) still counts.
        recovered = [
            issue
            for issue in summary.convergence_issues
            if any(marker in issue.lower() for marker in _STEPPING_FAILURES)
        ]
        summary.convergence_issues = [
            issue for issue in summary.convergence_issues if issue not in recovered
        ]
        summary.warnings.extend(f"recovered: {issue}" for issue in recovered)
    return summary


def _looks_like_path(text: str) -> bool:
    if len(text) < 4 or text.endswith(":"):
        return False
    return ("\\" in text or "/" in text) and not text.endswith("=")


def _is_error_line(stripped: str, lowered: str) -> bool:
    match = _FILELINE_RE.match(stripped)
    if match:
        return True
    if lowered.startswith(".meas") or lowered.startswith("measurement"):
        # ".meas" failure text is handled by the measurement parser.
        return "failed" not in lowered and "error" not in lowered
    return any(marker in lowered for marker in _ERROR_MARKERS)


def _parse_measurement(stripped: str) -> Measurement | None:
    if stripped.startswith("*") or stripped.startswith("."):
        return None

    failed = _MEAS_FAILED_RE.match(stripped)
    if failed and ("meas" in stripped.lower() or "=" in stripped or ":" in stripped):
        # e.g. 't_rise: TRIG ... FAILED'
        return Measurement(
            name=failed.group("name").strip(),
            value=None,
            target=(failed.group("target") or "").strip() or None,
            at_s=None,
            failed=True,
            text=stripped,
        )

    ok = _MEAS_OK_RE.match(stripped)
    if not ok:
        return None
    name = ok.group("name").strip()
    if not name or name.lower() in {"circuit", "start time", "tnom", "temp", "method", "solver"}:
        return None
    target = ok.group("target").strip() or None
    value = float(ok.group("value"))
    tail = ok.group("tail") or ""
    at_s: float | None = None
    at_match = _MEAS_AT_RE.search(tail)
    if at_match:
        at_s = float(at_match.group("at"))
    elif target:
        target_at = _TARGET_AT_RE.match(target)
        if target_at:
            at_s = float(target_at.group("at"))
    return Measurement(
        name=name, value=value, target=target, at_s=at_s, failed=False, text=stripped
    )
