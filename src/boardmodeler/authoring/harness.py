"""The deterministic judge: real LTspice runs in, PASS/FAIL/UNKNOWN out.

Honesty rule this module implements: a PASS always comes from an observed
artifact of a completed run. The chain is

1. the model library is read and its declared ports are resolved,
2. one deck per bound probe is rendered and run in ``<workdir>/probes/<id>/``,
3. the run is accepted only when the log completed, the ``.raw`` is readable,
   no convergence failure occurred and the waveform reached the declared stop
   time (all through :mod:`boardmodeler.simulation.measures`), and
4. the probe's own measurement must exist and be finite.

Any failure in that chain is UNKNOWN with the observed reason — never FAIL
(a model is not blamed for a simulator that did not deliver data) and never
PASS. Limits stay the harness's: the deck parameters and limits come from the
frozen :class:`~boardmodeler.authoring.spec.SpecSet`, so an authoring agent can
move its model but not the target.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from boardmodeler.authoring.probes import PROBES, ProbeError, judge_value, model_ports
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.domain.enums import RequirementClass, Status
from boardmodeler.domain.hashing import sha256_file
from boardmodeler.simulation.deck import TranSpec
from boardmodeler.simulation.log import parse_log
from boardmodeler.simulation.ltspice import BatchResult, run_batch
from boardmodeler.simulation.measures import diagnose
from boardmodeler.simulation.raw import RawFile, RawFormatError, read_raw

__all__ = ["HarnessReport", "ProbeOutcome", "judge_characteristic", "run_harness"]

#: Relative slack applied to a declared limit before it is called a violation:
#: one part per million of the limit magnitude absorbs ``.raw`` float rounding.
#: The detail always prints the unrounded measured value and the exact limit, so
#: the slack can never hide a real margin.
_LIMIT_SLACK = 1e-6

#: Fraction of a typical value that counts as "matching the datasheet typical".
_TYPICAL_TOLERANCE = 0.10


#: Outcome reason for a case where some rows declare no numeric limit: the measurement
#: is valid and still judges the rows that do.
_NO_NUMERIC_LIMIT_REASON = "characteristic_without_numeric_limit"


@dataclass(frozen=True)
class ProbeOutcome:
    """What one probe measured, and what the harness concluded from it."""

    probe_id: str
    status: str
    measured: dict[str, float | str]
    detail: str
    unknown_reason: str | None
    run_dir: str
    char_ids: tuple[str, ...]
    judged: str = ""
    citations: tuple[str, ...] = ()
    cause: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    operating_point: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "status": self.status,
            "measured": {key: value for key, value in sorted(self.measured.items())},
            "detail": self.detail,
            "unknown_reason": self.unknown_reason,
            "run_dir": self.run_dir,
            "char_ids": list(self.char_ids),
            "judged": self.judged,
            "citations": list(self.citations),
            "cause": self.cause,
            "artifacts": self.artifacts,
            "operating_point": self.operating_point,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> ProbeOutcome:
        measured = payload.get("measured") or {}
        return cls(
            probe_id=str(payload["probe_id"]),
            status=str(payload["status"]),
            measured={str(k): float(v) for k, v in dict(measured).items()},
            detail=str(payload.get("detail", "")),
            unknown_reason=(
                None if payload.get("unknown_reason") is None else str(payload["unknown_reason"])
            ),
            run_dir=str(payload.get("run_dir", "")),
            char_ids=tuple(str(x) for x in payload.get("char_ids") or ()),
            judged=str(payload.get("judged", "")),
            citations=tuple(str(x) for x in payload.get("citations") or ()),
            cause=None if payload.get("cause") is None else str(payload["cause"]),
            artifacts=dict(payload.get("artifacts") or {}),
            operating_point=dict(payload.get("operating_point") or {}),
        )


@dataclass(frozen=True)
class HarnessReport:
    """Every probe outcome for one model revision, plus the identity hashes."""

    part: str
    model_sha256: str
    spec_digest: str
    outcomes: tuple[ProbeOutcome, ...]

    def payload(self) -> dict:
        return {
            "part": self.part,
            "model_sha256": self.model_sha256,
            "spec_digest": self.spec_digest,
            "outcomes": [outcome.to_json() for outcome in self.outcomes],
        }

    def counts(self) -> dict[str, int]:
        """Outcome count per status value (all five keys, stable order)."""
        tally = {status.value: 0 for status in Status}
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    def passed(self) -> bool:
        """True only when every outcome PASSed and at least one characteristic was judged."""
        judged = sum(len(outcome.char_ids) for outcome in self.outcomes)
        return (
            bool(self.outcomes)
            and judged > 0
            and all(outcome.status == Status.PASS.value for outcome in self.outcomes)
        )

    def failing(self) -> tuple[ProbeOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == Status.FAIL.value)

    def unknown(self) -> tuple[ProbeOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == Status.UNKNOWN.value)

    def feedback(self) -> str:
        """Compact text for the authoring agent: what failed, and against what.

        Failures come first, then runs that could not answer (UNKNOWN). Each
        block names the probe, the measured number and unit, the datasheet
        requirement with its page and verbatim excerpt, and — when the harness
        can tell — one sentence on the likely cause. An empty string means every
        bound characteristic passed.
        """
        blocks: list[str] = []
        for outcome in self.failing():
            blocks.append(f"probe {outcome.probe_id} [FAIL]")
            blocks.append(f"  measured: {outcome.judged or '(see measured map)'}")
            for citation in outcome.citations:
                blocks.append(f"  datasheet: {citation}")
            if outcome.cause:
                blocks.append(f"  likely cause: {outcome.cause}")
        deferred = []
        for outcome in self.unknown():
            if (outcome.unknown_reason or "").startswith("deferred_after_invalid_simulation:"):
                deferred.extend(outcome.char_ids)
                continue
            blocks.append(f"probe {outcome.probe_id} [UNKNOWN]")
            blocks.append(f"  reason: {outcome.unknown_reason or '(unspecified)'}")
            for citation in outcome.citations:
                blocks.append(f"  would have required: {citation}")
        if deferred:
            blocks.append(
                "Remaining fixtures deferred until the simulation error is repaired (all limits stay frozen): "
                + ", ".join(deferred)
            )
        return "\n".join(blocks)

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> HarnessReport:
        payload = json.loads(text)
        return cls(
            part=str(payload["part"]),
            model_sha256=str(payload["model_sha256"]),
            spec_digest=str(payload["spec_digest"]),
            outcomes=tuple(ProbeOutcome.from_json(entry) for entry in payload.get("outcomes", [])),
        )


def _citation(char: Characteristic) -> str:
    """One line: what the datasheet requires, where it is written, verbatim."""
    where = f"PDF page {char.source_page}" if char.source_page is not None else "page not cited"
    excerpt = f' — "{char.excerpt}"' if char.excerpt else ""
    return f"{char.char_id} requires {char.limits_text()} ({where}){excerpt}"


def _unknown_outcome(
    probe_id: str,
    run_dir: Path,
    chars: tuple[Characteristic, ...],
    reason: str,
) -> ProbeOutcome:
    """An outcome for a probe that could not deliver a judged value."""
    return ProbeOutcome(
        probe_id=probe_id,
        status=Status.UNKNOWN.value,
        measured={},
        detail="",
        unknown_reason=reason,
        run_dir=str(run_dir),
        char_ids=tuple(char.char_id for char in chars),
        citations=tuple(_citation(char) for char in chars),
    )


def _judge(char: Characteristic, key: str, value: float) -> tuple[str, str, str | None]:
    """``(status, detail, likely cause)`` for one characteristic and one measurement."""
    label = f"{char.char_id} [{char.req_class}]"
    judged = f"{key}={value:.6g} {char.unit}".strip()
    if char.has_limits:
        slack = _LIMIT_SLACK * max(abs(char.min_value or 0.0), abs(char.max_value or 0.0), 1e-12)
        if char.min_value is not None and value < char.min_value - slack:
            return (
                Status.FAIL.value,
                f"{label}: measured {judged} is below the required minimum "
                f"{char.min_value:g} {char.unit}",
                f"measured {value:.6g} {char.unit} is {char.min_value - value:.6g} {char.unit} "
                f"below the {char.min_value:g} {char.unit} minimum: the model's {key} is too low",
            )
        if char.max_value is not None and value > char.max_value + slack:
            return (
                Status.FAIL.value,
                f"{label}: measured {judged} exceeds the required maximum "
                f"{char.max_value:g} {char.unit}",
                f"measured {value:.6g} {char.unit} is {value - char.max_value:.6g} {char.unit} "
                f"above the {char.max_value:g} {char.unit} maximum: the model's {key} is too high",
            )
        return (
            Status.PASS.value,
            f"{label}: measured {judged} within {char.limits_text()}",
            None,
        )
    if char.typ_value is not None:
        tolerance = _TYPICAL_TOLERANCE * abs(char.typ_value)
        deviation = abs(value - char.typ_value)
        note = (
            "typical-value comparison, not a min/max limit check"
            if char.req_class == RequirementClass.TYPICAL_VALUE.value
            else f"only a typical value is extracted for this {char.req_class} statement"
        )
        detail = (
            f"{label}: measured {judged}, |measured - typ {char.typ_value:g}| = "
            f"{deviation:.6g} {char.unit} versus the 10 % band {tolerance:.6g} {char.unit} ({note})"
        )
        cause = None
        if deviation > tolerance:
            cause = (
                f"measured {value:.6g} {char.unit} deviates {deviation:.6g} {char.unit} from the "
                f"{char.typ_value:g} {char.unit} typical (10 % band {tolerance:.6g} {char.unit}): "
                f"the model's {key} is not centred on the datasheet value"
            )
        return (Status.PASS.value if cause is None else Status.FAIL.value, detail, cause)
    return (
        Status.UNKNOWN.value,
        f"{label}: the characteristic declares no numeric limit to judge {key} against",
        None,
    )


def judge_characteristic(characteristic: Characteristic, outcome: ProbeOutcome) -> tuple[str, str]:
    """``(status, detail)`` for one characteristic judged from its probe's measurement.

    One probe case can carry several characteristics at the same operating point, and the
    outcome's aggregate status is the worst of them; re-judging from the same measured
    number keeps every row's verdict its own. An unavailable or invalid run stays UNKNOWN
    (or BLOCKED) even if a partial measurement is present, so it is never upgraded to
    PASS; only a valid shared measurement that a sibling row could not use (no numeric
    limit) is re-judged for the rows that can.
    """
    if outcome.status in (Status.UNKNOWN.value, Status.BLOCKED.value) and (
        outcome.unknown_reason != _NO_NUMERIC_LIMIT_REASON
    ):
        return outcome.status, outcome.detail
    try:
        key, value = judge_value(outcome.probe_id, outcome.measured)
    except ProbeError, ValueError:
        return outcome.status, outcome.detail
    status, detail, _cause = _judge(characteristic, key, value)
    return status, detail


def _simulator_said(log) -> str:
    """The last lines the simulator printed to its own log, or an empty string.

    A reason that says only "no usable output" leaves the author blind: the deck's own
    error line (an LTspice syntax error inside the ``.subckt``, an undefined sub-model, an
    over-defined matrix) is what lets the next turn fix the model. ``log`` is the parsed
    log summary; anything it kept as a diagnostic is worth repeating, in the order the
    reasons themselves prefer (errors, then convergence, then warnings).
    """
    said: list[str] = []
    for name in ("errors", "convergence_issues", "warnings"):
        said.extend(str(line) for line in (getattr(log, name, None) or []))
    if not said:
        return ""
    tail = list(dict.fromkeys(said))[:2]
    return f"; LTspice said: {' | '.join(tail)[:300]}"


def _run_reason(
    result: BatchResult, log, *, tstop_s: float, tmax_s: float, analysis: str = "tran"
) -> str | None:
    """Why this run cannot produce a verdict, or ``None`` when it delivered data."""
    if result.cancelled:
        return "cancelled"
    if result.timed_out:
        return f"run_timeout: {result.observed()}"
    raw: RawFile | None = None
    raw_error: str | None = None
    if result.raw_path is not None and result.raw_path.is_file():
        try:
            raw = read_raw(result.raw_path)
        except (RawFormatError, OSError) as exc:
            # An empty or truncated .raw means the run ended before it produced data, and
            # the simulator's log is the only place that says why.
            raw_error = f"{exc}{_simulator_said(log)}"
    else:
        raw_error = f"no .raw was written ({result.observed()}){_simulator_said(log)}"
    diag = diagnose(
        log=log,
        raw=raw,
        tran=(
            TranSpec(tstep=0.0, tstop=tstop_s, tstart=0.0, tmax=tmax_s)
            if analysis == "tran"
            else None
        ),
        raw_error=raw_error,
    )
    return diag.blocked_reason()


def run_harness(
    *,
    model_lib: Path,
    subckt: str,
    spec: SpecSet,
    workdir: Path,
    ltspice: Path,
    timeout_s: float = 120.0,
    cancel: threading.Event | None = None,
) -> HarnessReport:
    """Run every probe the spec binds, in ``<workdir>/probes/<probe_id>/``.

    ``timeout_s`` is the per-run limit handed to the simulator adapter. A
    cancelled harness stops before launching the next probe; probes that were
    never run are reported UNKNOWN with reason ``cancelled`` so that a partial
    report is never mistaken for a passing one.
    """
    model_lib = Path(model_lib)
    workdir = Path(workdir)
    probes_root = workdir / "probes"
    probes_root.mkdir(parents=True, exist_ok=True)
    model_sha = sha256_file(model_lib) if model_lib.is_file() else ""

    library_error: str | None = None
    try:
        from boardmodeler.authoring.model_syntax import validate_library

        model_ports(model_lib, subckt)
        validate_library(model_lib)
    except ProbeError as exc:
        library_error = exc.full_reason()

    outcomes: list[ProbeOutcome] = []
    for case_id, probe_id, chars in spec.cases():
        run_dir = probes_root / case_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if library_error is not None:
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, library_error))
            continue
        if cancel is not None and cancel.is_set():
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, "cancelled"))
            continue

        probe_params = [char.probe_params for char in chars]
        try:
            probe = PROBES[probe_id]
            if probe_id == "circuit_measurement":
                from boardmodeler.authoring.circuit_probe import make_probe

                probe = make_probe(chars[0].probe_recipe)
        except KeyError:
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, f"unknown_probe:{probe_id}"))
            continue
        except ValueError as exc:
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, f"invalid_recipe:{exc}"))
            continue

        try:
            test_model, test_subckt = model_lib, subckt
            if chars[0].probe_ports:
                from boardmodeler.authoring.pin_roles import write_probe_adapter

                test_model, test_subckt = write_probe_adapter(
                    model_lib,
                    subckt,
                    chars[0].probe_ports,
                    spec.pin_map,
                    run_dir / "fixture-adapter.lib",
                )
            deck_text = probe.render(
                model_lib=test_model, subckt=test_subckt, params=probe_params[0]
            )
        except (ProbeError, ValueError) as exc:
            reason = exc.full_reason() if isinstance(exc, ProbeError) else str(exc)
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, reason))
            continue

        deck = run_dir / "deck.cir"
        deck.write_text(deck_text, encoding="utf-8", newline="\n")
        result = run_batch(ltspice, deck, run_dir, timeout_s=timeout_s)
        log = parse_log(result.log_path) if result.log_path is not None else None
        params = probe.merged_params(probe_params[0])
        reason = (
            _run_reason(
                result,
                log,
                tstop_s=params["tstop_s"],
                tmax_s=params["tmax_s"],
                analysis=probe.analysis,
            )
            if log is not None
            else f"run_incomplete: {result.observed()}"
        )
        if reason is not None:
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, reason))
            if reason.startswith(
                ("run_timeout:", "sim_convergence_failure:", "sim_output_unreadable:")
            ):
                # Repair an invalid candidate before spending another timeout on
                # every remaining fixture. All deferred rows remain UNKNOWN and
                # are run normally after a changed candidate is supplied.
                library_error = "deferred_after_invalid_simulation: " + reason
            continue

        try:
            measured = probe.measure(result.raw_path, probe_params[0])
            key, value = judge_value(probe_id, measured)
        except ProbeError as exc:
            outcomes.append(_unknown_outcome(probe_id, run_dir, chars, exc.full_reason()))
            continue

        verdicts: list[str] = []
        status = Status.PASS.value
        cause: str | None = None
        for char in chars:
            char_status, detail, char_cause = _judge(char, key, value)
            verdicts.append(detail)
            if char_status == Status.FAIL.value:
                status = Status.FAIL.value
                cause = cause or char_cause
            elif char_status == Status.UNKNOWN.value and status != Status.FAIL.value:
                status = Status.UNKNOWN.value
        citations = tuple(_citation(char) for char in chars)
        outcomes.append(
            ProbeOutcome(
                probe_id=probe_id,
                status=status,
                measured={name: float(value) for name, value in measured.items()},
                detail="; ".join(verdicts),
                unknown_reason=(
                    _NO_NUMERIC_LIMIT_REASON if status == Status.UNKNOWN.value else None
                ),
                run_dir=str(run_dir),
                char_ids=tuple(char.char_id for char in chars),
                judged=f"{key} = {value:.6g} {chars[0].unit}".strip(),
                citations=citations,
                cause=cause,
                operating_point=(
                    {
                        **chars[0].probe_recipe.get("operating_point", {}),
                        "temperature_C": chars[0].probe_recipe.get("temperature", 25),
                        "tstop_s": chars[0].probe_recipe.get("stop"),
                        "tmax_s": chars[0].probe_recipe.get("step"),
                    }
                    if chars[0].probe_recipe
                    else dict(params)
                ),
                artifacts={
                    str(path.resolve()): sha256_file(path)
                    for path in (result.raw_path, result.log_path)
                    if path is not None
                },
            )
        )

    return HarnessReport(
        part=spec.part,
        model_sha256=model_sha,
        spec_digest=spec.digest(),
        outcomes=tuple(outcomes),
    )
