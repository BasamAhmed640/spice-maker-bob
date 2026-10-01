"""Datasheet + part number + agent key -> a judged LTspice model, saved for LTspice.

This module is the single entry point the model maker calls. One run walks six
stages and never invents a result:

``read``
    The picked PDF is registered as a :class:`DocumentRecord` in
    ``<out_dir>/build`` (classification defaults to ``unknown``; the caller decides
    egress with ``allow_remote``) and the requirement rows come either from
    ``requirements_json`` (validated, never re-interpreted) or from the configured
    extraction provider. A part no probe can judge — a microcontroller or a
    programmable-logic device, see :mod:`boardmodeler.authoring.part_class` —
    stops here with ``BLOCKED(unsupported_part_class: ...)``, before any agent
    turn or simulation is spent on it.

``extract``
    Provider-driven extraction to the D4 records, then the deterministic
    validation and citation review. A provider that is unavailable is reported
    with its own reason — D-011, no silent fallback to another provider. Responses
    are content-addressed under ``<out_dir>/cache``, so a repeat run over the same
    document makes zero inference requests.

``bind``
    A deterministic keyword binder maps each requirement row to one probe of the
    :data:`~boardmodeler.authoring.probes.PROBES` registry, or declares it
    ``not_testable`` with a concrete reason. The result is written to
    ``<out_dir>/spec/bindings.json`` so the user can review (and later replay,
    via ``bindings_json``) exactly what a run was judged against. Rows whose
    citation cannot be re-verified against the cited page are never bound: they
    stay ``UNKNOWN``.

``author`` / ``judge``
    ``prepare_workdir`` freezes the spec, the agent writes the model, and
    ``build_model`` runs the real harness after every turn — until the harness is
    satisfied, until the agent stops improving, or until the caller's iteration
    cap, whichever comes first. Each harness turn is reported to ``progress`` as a
    ``judge`` event carrying the turn number and the harness's own counts, so a
    GUI can show "turn 2 — vref too low".

``save``
    ``<subckt>.lib``, ``<subckt>.asy`` (the agent's symbol is validated and
    regenerated when it is not a bijection), ``MODEL_CARD.md``, ``EXAMPLE.cir``,
    ``harness-report.json``, ``spec/`` and ``results.json`` land in ``out_dir``.

Status ladder: ``PASS`` only when every bound row passed a real simulator run;
``BLOCKED`` when the backend, the provider or LTspice was unavailable, or when the
part is one no probe can judge; ``FAIL`` only when the harness measured a fully
judged model wrong at the iteration cap;
``UNKNOWN`` for everything else (a cancelled run, an abandoned model file, a
tampered spec, an agent that stopped making progress, rows the harness could not
judge). ``detail`` always names the concrete next action for a human.

The author loop runs until the agent is done, not until a clock stops it: there
is no build deadline. ``max_iterations`` is ``None`` by default — no cap — and the
loop stops on satisfaction, on ``max_iterations`` when a caller sets one, or after
``stall_patience`` consecutive turns that change nothing the harness can see; a
capped or stalled run still reports every row the harness measured. The product
``api`` path applies a finite default of
:data:`~boardmodeler.authoring.api_backend.DEFAULT_TIMEOUT_S` (600 s) to each
turn when the caller leaves ``turn_timeout_s`` unset; an explicit value overrides
it, and the loop API and the Bob-only edition's direct CLI path stay unbounded
when called with ``None``. ``timeout_s`` bounds a single simulation run, not the
build.

Nothing raises for an expected failure — a missing datasheet, a document the
provider refuses, a missing key, absent LTspice, a tampered spec or a
cancellation all come back as a status with the observed reason. Only
programming errors (an unsanitised subcircuit name, a non-positive iteration cap)
raise.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from boardmodeler.authoring.api_backend import DEFAULT_TIMEOUT_S as DEFAULT_API_TIMEOUT_S
from boardmodeler.authoring.api_backend import build_api_backend
from boardmodeler.authoring.backends import (
    AuthorBackend,
    BobShellBackend,
    ScriptedBackend,
    UnavailableBackend,
)
from boardmodeler.authoring.card import status_tally, write_deliverables, write_symbol_for
from boardmodeler.authoring.harness import HarnessReport, judge_characteristic
from boardmodeler.authoring.loop import (
    BuildOutcome,
    BuildRequest,
    build_model,
    model_file,
    prepare_workdir,
    revalidate_candidate,
)
from boardmodeler.authoring.part_class import classify
from boardmodeler.authoring.probes import PROBES
from boardmodeler.authoring.reinforce import ReinforcementReport, reinforce
from boardmodeler.authoring.spec import SpecSet, load_tps54320_spec, normalize_unit
from boardmodeler.build_flavor import BOB_ONLY
from boardmodeler.config import load_config
from boardmodeler.documents.pdf import page_text, read_pdf, read_pdf_pdfium
from boardmodeler.documents.store import DocumentStore, DocumentStoreError
from boardmodeler.domain.enums import RequirementClass, RequirementOrigin, Status
from boardmodeler.domain.records import DocumentRecord, Requirement
from boardmodeler.models.library import ModelStoreError, subckt_ports
from boardmodeler.models.symbolism import symbol_text
from boardmodeler.providers.agent import AgentExtractionProvider
from boardmodeler.providers.base import ProviderError
from boardmodeler.providers.registry import select_provider
from boardmodeler.requirements.model import validate_requirements
from boardmodeler.requirements.review import READING_BREAK, apply_review, verify_citations
from boardmodeler.security.network import (
    NetworkRefused,
    internet_allowed,
    refusal_detail,
    require_network,
)
from boardmodeler.simulation.ltspice import locate

if BOB_ONLY:
    _API_BACKEND_TYPES = ()
else:
    from boardmodeler.authoring.api_backend import ApiKeyBackend

    _API_BACKEND_TYPES = (ApiKeyBackend,)

__all__ = [
    "MakeModelRequest",
    "MakeModelResult",
    "ModelSummary",
    "RowOutcome",
    "StageEvent",
    "bind_requirements",
    "build_backend",
    "load_model_summary",
    "make_model",
]

STAGES: tuple[str, ...] = ("read", "extract", "bind", "author", "judge", "save")

WORK_DIRNAME = "build"
"""Agent sandbox, extraction workspace and harness output, under ``out_dir``."""

CACHE_DIRNAME = "cache"
"""Content-addressed extraction responses, under ``out_dir``."""

SPEC_DIRNAME = "spec"
REQUIREMENTS_NAME = "requirements.json"
BINDINGS_NAME = "bindings.json"
CHARACTERISTICS_NAME = "characteristics.json"
HARNESS_REPORT_NAME = "harness-report.json"
RESULTS_NAME = "results.json"
TIMING_NAME = "run-timing.json"
SUPPORT_RECORD_NAME = "support-decision.json"
QUALIFICATION_REPORT_NAME = "qualification-report.json"
QUALIFICATION_PLAN_NAME = "qualification-plan.json"
PINOUT_REPORT_NAME = "pinout-report.json"
PINOUT_CONTRACT_NAME = "pinout-contract.json"
ENGINES = ("legacy_ai", "behavioral", "pin_only")
DESIGN_RECORD_NAME = "model-design.json"
EXAMPLE_NAME = "EXAMPLE.cir"

LTSPICE_MISSING = (
    "ltspice_not_found: LTspice is not configured; open SETUP and choose "
    "LTspice.exe, then re-run 'boardmodeler doctor' to confirm it"
)

_TEXT_SUFFIXES = frozenset({".txt", ".text", ".md"})

#: The loop reports an exhausted iteration budget with this prefix; it is the
#: only terminal reason that means "the agent ran out of turns with the harness
#: still measuring the model wrong" rather than "the build stopped early".
_CAP_PREFIX = "max_iterations="

#: One process runs one model build at a time; a second concurrent call is
#: reported rather than interleaving the builds' authoring work.
_BUILD_LOCK = threading.RLock()


def _exclusive_model_build(function):
    """Serialize the complete build, including output withdrawal and local routes."""

    @wraps(function)
    def wrapped(request, progress=None, cancel=None):
        checked = _checked(request)
        if not _BUILD_LOCK.acquire(blocking=False):
            detail = (
                "build_in_progress: another model build is running in this process; "
                "wait for it to finish and re-run"
            )
            log = _StageLog(progress)
            log.emit("read", "failed", detail)
            return _empty_result(checked, log, detail)
        try:
            return function(checked, progress=progress, cancel=cancel)
        finally:
            _BUILD_LOCK.release()

    return wrapped


_SUBCKT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The physical unit of the number each probe judges, by its ``judge_key``. The
#: binder refuses to bind a row whose extracted unit disagrees, because the
#: harness compares the row's limits against exactly this number.
_JUDGE_UNITS: dict[str, str] = {
    "vin_at_start": "V",
    "vin_at_stop": "V",
    "en_at_start": "V",
    "en_at_stop": "V",
    "v_fb": "V",
    "i_heavy": "A",
    "i_out_limit": "A",
    "i_vin_a": "A",
    "pg_leak_a": "A",
    "t_ss_s": "s",
    "io_voltage_v": "V",
    "io_current_a": "A",
    "io_time_s": "s",
}


def _probe_units(probe_id: str) -> str:
    """The unit of the number ``probe_id`` judges, or ``""`` when undeclared."""
    probe = PROBES.get(probe_id)
    if probe is None:
        return ""
    if probe.judge_key == "pwm_value":
        return probe.unit
    return _JUDGE_UNITS.get(probe.judge_key, "")


# --------------------------------------------------------------------------- #
# records


@dataclass(frozen=True)
class MakeModelRequest:
    """What the caller asks for: a part, its datasheet, and where to save it.

    ``max_iterations=None`` (the default) has no cap: the author loop runs until
    the harness is satisfied or the agent stops making progress, which is what
    ``stall_patience`` counts. ``turn_timeout_s`` bounds one agent invocation; when
    it is ``None`` the product ``api`` path applies its own finite 600 s default, and
    an explicit value overrides that. ``timeout_s`` bounds a single simulation run.
    There is no build deadline.

    ``backend_name`` names the author: ``"api"`` (the default) uses an API-key
    provider — ``provider`` is a provider id from
    :mod:`boardmodeler.agent_providers`, ``agent_model`` overrides its
    documented model and ``agent_max_tokens`` its output budget, all falling back
    to the persisted settings and then to the catalog's own defaults — and
    ``"scripted"``/``"fixture"`` write the bundled offline template. ``"bob"`` names
    the Bob-only edition's CLI, which a general build refuses with a reason.
    """

    part: str
    subckt: str
    datasheet: Path
    out_dir: Path
    backend_name: str = "api"
    provider: str | None = None
    agent_model: str | None = None
    agent_max_tokens: int | None = None
    max_iterations: int | None = None
    timeout_s: float = 120.0
    allow_remote: bool = False
    team_id: str | None = None
    requirements_json: Path | None = None
    bindings_json: Path | None = None
    turn_timeout_s: float | None = None
    stall_patience: int = 2
    #: Search the web for supporting material before the agent starts. ``None`` follows
    #: the persistent setting; ``True``/``False`` override it for one run. Supporting
    #: material never feeds a verdict -- the datasheet rows remain the only oracle.
    reinforce: bool | None = None
    #: Budget for the supporting-material search only. ``None`` leaves it unbounded; the
    #: author loop is never bounded by this (``max_iterations``/``turn_timeout_s`` stay
    #: ``None``), so the search cannot weaken a tested claim.
    reinforce_timeout_s: float | None = 45.0
    #: Low-level API remains full by default; the GUI defaults to sanity mode.
    verification: str = "full"
    #: Which generation route may run. behavioral is the code-built default with no
    #: agent authoring turn and runs only for a
    #: part the support decision marks supported; pin_only is separately requested and
    #: limited. Every route is refused for a blocked or unclassified part, and none is a
    #: fallback for another.
    engine: str = "behavioral"
    #: What kind of part this is, when the operator knows and the datasheet does not say
    #: (a family id from models.support). It never unblocks a class and never makes a part
    #: supported; it only names the family for a part nothing else identifies.
    family: str | None = None
    #: Let the agent plan the extra test circuits on a local route (behavioral, pin_only).
    #: Off by default: the local routes bind with the reviewed keyword table and ask no
    #: provider. The plan is frozen into spec/bindings.json, so a later run replays it with
    #: bindings_json and asks nothing.
    plan_tests: bool = False


@dataclass(frozen=True)
class StageEvent:
    """One progress report: which stage, how it stands, and what it produced."""

    stage: str
    status: str
    detail: str
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RowOutcome:
    """One datasheet row and what happened to it (the GUI's table row)."""

    req_id: str
    statement: str
    required: str
    measured: str
    status: str
    page: int | None


@dataclass(frozen=True)
class MakeModelResult:
    """The run's verdict, its per-row outcome, and the files it published."""

    status: str
    detail: str
    part: str
    out_dir: Path
    card_path: Path | None
    lib_path: Path | None
    asy_path: Path | None
    rows: tuple[RowOutcome, ...]
    counts: dict[str, int]
    stages: tuple[StageEvent, ...]
    #: The parameters this result came from, so a saved ``results.json`` records
    #: (and :meth:`from_json` restores) exactly the run that produced it —
    #: including ``max_iterations``, ``stall_patience`` and ``turn_timeout_s``.
    request: MakeModelRequest | None = None

    def to_json(self) -> str:
        payload = {
            "status": self.status,
            "detail": self.detail,
            "part": self.part,
            "out_dir": str(self.out_dir),
            "card_path": None if self.card_path is None else str(self.card_path),
            "lib_path": None if self.lib_path is None else str(self.lib_path),
            "asy_path": None if self.asy_path is None else str(self.asy_path),
            "counts": {key: self.counts[key] for key in sorted(self.counts)},
            "rows": [
                {
                    "req_id": row.req_id,
                    "statement": row.statement,
                    "required": row.required,
                    "measured": row.measured,
                    "status": row.status,
                    "page": row.page,
                }
                for row in self.rows
            ],
            "stages": [
                {
                    "stage": event.stage,
                    "status": event.status,
                    "detail": event.detail,
                    "counts": {key: event.counts[key] for key in sorted(event.counts)},
                }
                for event in self.stages
            ],
            "request": None if self.request is None else _request_payload(self.request),
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> MakeModelResult:
        """The result a :meth:`to_json` payload describes (rows, stages, request)."""
        payload = json.loads(text)
        request = payload.get("request")
        return cls(
            status=str(payload["status"]),
            detail=str(payload["detail"]),
            part=str(payload["part"]),
            out_dir=Path(payload["out_dir"]),
            card_path=_optional_path(payload.get("card_path")),
            lib_path=_optional_path(payload.get("lib_path")),
            asy_path=_optional_path(payload.get("asy_path")),
            rows=tuple(
                RowOutcome(
                    req_id=str(row["req_id"]),
                    statement=str(row.get("statement", "")),
                    required=str(row.get("required", "")),
                    measured=str(row.get("measured", "")),
                    status=str(row["status"]),
                    page=None if row.get("page") is None else int(row["page"]),
                )
                for row in payload.get("rows", ())
            ),
            counts={str(key): int(value) for key, value in (payload.get("counts") or {}).items()},
            stages=tuple(
                StageEvent(
                    stage=str(event["stage"]),
                    status=str(event["status"]),
                    detail=str(event.get("detail", "")),
                    counts={
                        str(key): int(value) for key, value in (event.get("counts") or {}).items()
                    },
                )
                for event in payload.get("stages", ())
            ),
            request=None if request is None else _request_from_payload(request),
        )


def _request_payload(request: MakeModelRequest) -> dict[str, Any]:
    return {
        "part": request.part,
        "subckt": request.subckt,
        "datasheet": str(request.datasheet),
        "out_dir": str(request.out_dir),
        "backend_name": request.backend_name,
        "verification": request.verification,
        "engine": request.engine,
        "family": request.family,
        "plan_tests": request.plan_tests,
        "provider": request.provider,
        "agent_model": request.agent_model,
        "agent_max_tokens": (
            None if request.agent_max_tokens is None else int(request.agent_max_tokens)
        ),
        "max_iterations": None if request.max_iterations is None else int(request.max_iterations),
        "timeout_s": float(request.timeout_s),
        "allow_remote": bool(request.allow_remote),
        "team_id": request.team_id,
        "requirements_json": (
            None if request.requirements_json is None else str(request.requirements_json)
        ),
        "bindings_json": None if request.bindings_json is None else str(request.bindings_json),
        "turn_timeout_s": (
            None if request.turn_timeout_s is None else float(request.turn_timeout_s)
        ),
        "stall_patience": int(request.stall_patience),
        "reinforce": None if request.reinforce is None else bool(request.reinforce),
        "reinforce_timeout_s": (
            None if request.reinforce_timeout_s is None else float(request.reinforce_timeout_s)
        ),
    }


def _request_from_payload(payload: Mapping[str, Any]) -> MakeModelRequest:
    return MakeModelRequest(
        part=str(payload["part"]),
        subckt=str(payload["subckt"]),
        datasheet=Path(payload["datasheet"]),
        out_dir=Path(payload["out_dir"]),
        backend_name=str(payload.get("backend_name", "api")),
        verification=str(payload.get("verification", "full")),
        # Results written before engine selection existed used the agent route.
        engine=str(payload.get("engine", "legacy_ai")),
        family=None if payload.get("family") is None else str(payload["family"]),
        plan_tests=bool(payload.get("plan_tests", False)),
        provider=None if payload.get("provider") is None else str(payload["provider"]),
        agent_model=None if payload.get("agent_model") is None else str(payload["agent_model"]),
        agent_max_tokens=(
            None if payload.get("agent_max_tokens") is None else int(payload["agent_max_tokens"])
        ),
        max_iterations=(
            None if payload.get("max_iterations") is None else int(payload["max_iterations"])
        ),
        timeout_s=float(payload.get("timeout_s", 120.0)),
        allow_remote=bool(payload.get("allow_remote", False)),
        team_id=None if payload.get("team_id") is None else str(payload["team_id"]),
        requirements_json=_optional_path(payload.get("requirements_json")),
        bindings_json=_optional_path(payload.get("bindings_json")),
        turn_timeout_s=(
            None if payload.get("turn_timeout_s") is None else float(payload["turn_timeout_s"])
        ),
        stall_patience=int(payload.get("stall_patience", 2)),
        reinforce=None if payload.get("reinforce") is None else bool(payload["reinforce"]),
        reinforce_timeout_s=(
            None
            if payload.get("reinforce_timeout_s") is None
            else float(payload["reinforce_timeout_s"])
        ),
    )


def _optional_path(value: object) -> Path | None:
    return None if value is None else Path(str(value))


# --------------------------------------------------------------------------- #
# the binder
#
# The table is reviewed, ordered and explicit: the first rule whose phrases all
# match the requirement's own wording wins. Declines come before the probe rules
# they protect, because a statement that mentions a threshold but is not that
# threshold (a hysteresis width, a switch-internal limit, an application pull-up)
# must be declared untestable rather than stretched onto the nearest probe.


@dataclass(frozen=True)
class _Rule:
    """One reviewed binding decision."""

    name: str
    probe: str | None
    any_of: tuple[str, ...] = ()
    all_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()
    statement_none_of: tuple[str, ...] = ()
    reason: str = ""


_THERMAL = (
    "thermal shutdown",
    "thermal path",
    "thermal pad",
    "thermal resistance",
    "junction temperature",
    "overtemperature",
    "over-temperature",
)
_PACKAGE = ("package", "soldered", "solder", "mechanical", "footprint", "pin pitch")
_BOOT = ("bootstrap", "boot-ph", "boot and ph", "boot pin", "boot capacitor", "boot diode")
_OSCILLATOR = (
    "oscillator",
    "switching frequency",
    "switching-frequency",
    "rt/clk",
    "clock frequency",
    "frequency range",
)
_INTERNAL_SWITCH = (
    "high-side switch",
    "low-side switch",
    "switch current limit",
    "switch current",
    "internal switch",
)
_AMPLIFIER = (
    "transconductance",
    "error amplifier",
    "amplifier gain",
    "small-signal",
    "small signal",
    "dc gain",
)
_PIN_BIAS = (
    "bias current",
    "pin sources",
    "pin sourcing",
    "sources a current",
    "sourcing current",
    "pull-up current source",
    "pull-up current source",
)
_OPERATING_RANGE = (
    "operates from",
    "operating range",
    "operating-range",
    "power-stage input",
    "supply split",
    "control supply",
    "pvpin",
)

_IO_RULES = (
    _Rule(
        "paired delays",
        None,
        all_of=("tplh", "tphl"),
        reason="split rising and falling propagation delay into separate requirements",
    ),
    _Rule(
        "power-off leakage",
        "io_power_off_leakage",
        any_of=("ioff", "power-off leakage"),
        statement_none_of=("supply current", "supply-current"),
    ),
    _Rule("disabled output leakage", "io_leakage", any_of=("ioz", "three-state output leakage")),
    _Rule("input leakage", "io_input_leakage", any_of=("input leakage current",)),
    _Rule("output high", "io_voh", any_of=("voh", "high-level output voltage")),
    _Rule("output low", "io_vol", any_of=("vol", "low-level output voltage")),
    _Rule("rising propagation", "io_delay_rise", any_of=("tplh", "low-to-high propagation delay")),
    _Rule("falling propagation", "io_delay_fall", any_of=("tphl", "high-to-low propagation delay")),
    _Rule("output rise", "io_rise_time", any_of=("output rise time",)),
    _Rule("output fall", "io_fall_time", any_of=("output fall time",)),
)

_RULES: tuple[_Rule, ...] = (
    _Rule(
        name="thermal",
        probe=None,
        any_of=_THERMAL,
        reason=(
            "thermal behaviour is outside the model's scope: there is no thermal network, so "
            "junction temperature is not a simulated quantity"
        ),
    ),
    _Rule(
        name="package",
        probe=None,
        any_of=_PACKAGE,
        reason=(
            "package/mechanical requirement with no simulated electrical quantity; the "
            "harness measures waveforms, not assembly"
        ),
    ),
    _Rule(
        name="derived hysteresis",
        probe=None,
        any_of=("hysteresis",),
        reason=(
            "derived quantity: the hysteresis width is the difference between the rise and "
            "fall thresholds the harness measures separately, and the harness judges one "
            "measured number per characteristic, so the width is declared untestable"
        ),
    ),
    _Rule(
        name="power-good threshold ratio",
        probe=None,
        any_of=("pwrgd threshold", "power-good threshold", "power good threshold"),
        reason=(
            "the falling and rising power-good thresholds are extracted as one ratio band "
            "that no single measured edge answers, so the harness does not claim it"
        ),
    ),
    _Rule(
        name="power-good pull-up",
        probe=None,
        any_of=("pwrgd pull-up", "pwrgd pullup", "pwrgd pull up", "power-good pull-up"),
        reason=(
            "application-connectivity requirement on the surrounding circuit: the pull-up is "
            "a deck constant, not a quantity of the model under test"
        ),
    ),
    _Rule(
        name="power-good internal state",
        probe=None,
        any_of=("pulled low", "defined state"),
        reason=(
            "internal-state statement (a pin held low while an internal condition such as "
            "thermal shutdown, UVLO or soft-start holds): the reduced model exposes no such "
            "state to a pin-level measurement"
        ),
    ),
    _Rule(
        name="bootstrap path",
        probe=None,
        any_of=_BOOT,
        reason=(
            "BOOT/PH are not ports of the reduced model, so a bootstrap-path quantity cannot "
            "be observed at a pin"
        ),
    ),
    _Rule(
        name="oscillator",
        probe=None,
        any_of=_OSCILLATOR,
        reason=(
            "the reduced model has no oscillator (it regulates continuously), so an "
            "oscillator or RT/CLK frequency requirement cannot be observed"
        ),
    ),
    _Rule(
        name="switching cycles",
        probe=None,
        any_of=("hiccup", "switching cycles", "clock cycles"),
        reason=(
            "the retry is specified in cycles of an internal oscillator the model does not "
            "implement (the model's hiccup is a time constant), so a cycle count cannot be "
            "observed"
        ),
    ),
    _Rule(
        name="compensation node",
        probe=None,
        any_of=("comp node", "comp pin", "start-switching"),
        reason=(
            "the COMP node is internal to the reduced model and is not one of its ports, so "
            "its threshold cannot be measured"
        ),
    ),
    _Rule(
        name="soft-start pin",
        probe=None,
        any_of=("ss/tr", "ss pin", "soft-start pin", "soft start pin"),
        reason=(
            "SS/TR is not a port of the reduced model, so an internal-node quantity on it "
            "cannot be observed"
        ),
    ),
    _Rule(
        name="internal switch",
        probe=None,
        any_of=_INTERNAL_SWITCH,
        reason=(
            "the switch current path is internal to the converter: the reduced model has no "
            "switch branch, so a switch current limit cannot be observed"
        ),
    ),
    _Rule(
        name="amplifier internals",
        probe=None,
        any_of=_AMPLIFIER,
        reason=(
            "error-amplifier small-signal parameters are internal to the reduced model and "
            "the harness defines no pin-level measurement of them"
        ),
    ),
    _Rule(
        name="pin bias current",
        probe=None,
        any_of=_PIN_BIAS,
        reason=(
            "the model's input pins are high-impedance comparators with no bias-current "
            "source, so the datasheet's pin sourcing current cannot be observed"
        ),
    ),
    _Rule(
        name="operating range",
        probe=None,
        any_of=_OPERATING_RANGE,
        reason=(
            "operating-range/board-level statement, not a threshold: the harness measures one "
            "number at one condition, so an envelope is not converted into a PASS"
        ),
    ),
    _Rule(
        name="line regulation",
        probe=None,
        any_of=("line regulation", "line-regulation"),
        reason=(
            "line regulation needs a VIN sweep at fixed load; the load_regulation probe "
            "answers the load-step question and is not a substitute"
        ),
    ),
    _Rule(
        name="load regulation",
        probe="load_regulation",
        any_of=("load regulation", "load step", "output current", "output-current", "rated output"),
        none_of=("limit",),
    ),
    _Rule(
        name="current limit",
        probe="current_limit",
        any_of=(
            "current limit",
            "current-limit",
            "peak current",
            "overcurrent",
            "current limiting",
        ),
    ),
    _Rule(
        name="uvlo rising edge",
        probe="uvlo_rise",
        any_of=("uvlo", "under-voltage lockout", "under voltage lockout"),
        all_of=("rising",),
    ),
    _Rule(
        name="start-up threshold on vin",
        probe="uvlo_rise",
        any_of=("start-up threshold", "startup threshold", "start-up voltage", "startup voltage"),
        all_of=("vin",),
    ),
    _Rule(
        name="uvlo falling edge",
        probe="uvlo_fall",
        any_of=("uvlo", "under-voltage lockout", "under voltage lockout"),
        all_of=("falling",),
    ),
    _Rule(
        name="enable rising edge",
        probe="en_rise",
        any_of=(
            "en rising",
            "enable rising",
            "enable threshold rising",
            "en threshold rising",
            "enable turn-on",
            "en turn-on",
        ),
    ),
    _Rule(
        name="enable falling edge",
        probe="en_fall",
        any_of=(
            "en falling",
            "enable falling",
            "enable threshold falling",
            "en threshold falling",
            "enable turn-off",
            "en turn-off",
        ),
    ),
    _Rule(
        name="shutdown supply current",
        probe="shutdown_current",
        any_of=("shutdown",),
        all_of=("current",),
    ),
    _Rule(
        name="quiescent supply current",
        probe="quiescent_current",
        any_of=(
            "quiescent",
            "non-switching",
            "non switching",
            "operating supply current",
            "no-load supply current",
            "no load supply current",
            "supply current",
        ),
        none_of=("ioff", "power-off", "leakage"),
    ),
    _Rule(
        name="voltage reference",
        probe="vref",
        any_of=(
            "voltage reference",
            "reference voltage",
            "reference accuracy",
            "reference tolerance",
            "feedback reference",
            "regulation reference",
            "vref",
        ),
    ),
    _Rule(
        name="power-good pin",
        probe="pg_threshold",
        any_of=("pwrgd", "power-good", "power good", "pwr_good", "pg pin"),
    ),
    _Rule(
        name="soft start",
        probe="soft_start",
        any_of=("soft-start", "soft start", "start-up ramp", "startup ramp"),
    ),
)

_BINDING_NOTE = (
    "Datasheet requirements -> deterministic probe bindings from the reviewed keyword table in "
    "boardmodeler.pipeline.make_model. Every requirement id appears exactly once: either bound "
    "to a probe (with the deck parameters the probe needs) or declared not_testable with the "
    "concrete reason. A bound requirement must declare a numeric limit whose unit matches the "
    "quantity the probe measures."
)


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _mentions(text: str, phrase: str) -> bool:
    """Whether ``phrase`` appears in the normalized ``text`` as a whole phrase."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _search_text(requirement: Requirement) -> str:
    """The requirement's own wording: statement plus section/table, never the excerpt.

    The excerpt is the datasheet's surrounding text and routinely mentions other
    parameters (a shutdown-current row's excerpt contains the whole ENABLE AND
    UVLO table), so matching on it would bind rows to probes on the strength of a
    neighbouring line.
    """
    parts = [requirement.statement]
    for ref in requirement.evidence:
        parts.extend(part for part in (ref.section, ref.table, ref.figure) if part)
    return _normalized(" ".join(parts))


def _subject(requirement: Requirement) -> str:
    """A short, concrete label for a row no rule could bind."""
    text = " ".join(requirement.statement.split())
    return text if len(text) <= 90 else text[:87] + "..."


def _rule_matches(rule: _Rule, text: str) -> bool:
    if rule.all_of and not all(_mentions(text, phrase) for phrase in rule.all_of):
        return False
    if rule.any_of and not any(_mentions(text, phrase) for phrase in rule.any_of):
        return False
    return not (rule.none_of and any(_mentions(text, phrase) for phrase in rule.none_of))


def _has_limits(requirement: Requirement) -> bool:
    limits = requirement.limits
    if limits is None:
        return False
    return limits.min is not None or limits.typ is not None or limits.max is not None


_DASH_VARIANTS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_NON_INVERTING_SEPARATOR = re.compile(r"\bnon[\s\-]*inverting\b", re.IGNORECASE)
_SIGNAL_NODE = re.compile(r"^\s*[vi]\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$", re.IGNORECASE)
_SIGNAL_BARE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")

_POLARITY_NONINVERTING = re.compile(
    r"\bnon-?inverting\s+(?:output|buffer)\b|"
    r"\b(?:output|buffer)\s+is\s+non-?inverting\b|"
    r"\bA\s+high\s+gives\s+Y\s+high\b|"
    r"\bA\s+low\s+gives\s+Y\s+low\b",
    re.IGNORECASE,
)
_POLARITY_INVERTING = re.compile(
    r"(?<!non-)\binverting\s+(?:output|buffer)\b|"
    r"\b(?:output|buffer)\s+is\s+inverting\b|"
    r"\bA\s+high\s+gives\s+Y\s+low\b|"
    r"\bA\s+low\s+gives\s+Y\s+high\b",
    re.IGNORECASE,
)


def _polarity_text(text: str) -> str:
    """Fold non/inverting separators so whitespace and dashed spellings read as one word."""
    folded = "".join("-" if char in _DASH_VARIANTS else char for char in text)
    return _NON_INVERTING_SEPARATOR.sub("non-inverting", folded)


def _negative_text(text: str) -> str:
    """Fold dash variants and separator whitespace so ``supply-current`` reads as two words."""
    folded = "".join(" " if char in _DASH_VARIANTS or char == "-" else char for char in text)
    return " ".join(folded.split())


def _signal_names(signals: Sequence[str]) -> set[str]:
    """Bare node identities for single-node ``V(...)``/``I(...)`` references.

    The extractor writes node syntax (``V(Y)``) while datasheet prose writes the bare
    name (``Y``), so both are reduced to the node identity. A differential or multi-node
    expression is not one signal and is skipped: it cannot name this row's terminal.
    """
    names: set[str] = set()
    for signal in signals:
        text = str(signal).strip()
        node = _SIGNAL_NODE.match(text)
        if node is not None:
            names.add(node.group(1).lower())
            continue
        bare = _SIGNAL_BARE.match(text)
        if bare is not None:
            names.add(bare.group(1).lower())
    return names


def _cited_polarity(
    requirement: Requirement,
    requirements: Sequence[Requirement],
    blocked: Mapping[str, str],
) -> tuple[float | None, str | None, str | None]:
    """``(polarity, source_req_id, source_excerpt)`` the verified citations establish.

    A row that needs ``io_inverting`` may cite the polarity on a neighbouring functional
    row for the *same part and document*, but only when that row's citation is verified
    and its verbatim excerpt names this row's signal *and* asserts the output/buffer
    polarity (or an applicable input-to-output truth relation). An unverified citation,
    a generated statement or section title, another part or document, an unrelated
    signal, an input-pin-only description, and text stating both behaviours all leave
    the question open, so the row stays a declared gap rather than guessing.
    """
    target_docs = {ref.doc_id for ref in requirement.evidence}
    target_signals = _signal_names(requirement.signal_refs)
    if not target_docs or not target_signals:
        return None, None, None
    hints: dict[float, tuple[str, str]] = {}
    for source in requirements:
        if source.req_id in blocked or source.applies_to != requirement.applies_to:
            continue
        if not target_signals & _signal_names(source.signal_refs):
            continue
        for ref in source.evidence:
            if ref.doc_id not in target_docs:
                continue
            excerpt = _normalized(ref.excerpt or "")
            if not excerpt:
                continue
            if not any(
                re.search(rf"\b{re.escape(signal)}\b", excerpt, re.IGNORECASE)
                for signal in target_signals
            ):
                continue
            polarity_text = _polarity_text(excerpt)
            if _POLARITY_NONINVERTING.search(polarity_text):
                hints.setdefault(0.0, (source.req_id, excerpt))
            if _POLARITY_INVERTING.search(polarity_text):
                hints.setdefault(1.0, (source.req_id, excerpt))
    if len(hints) == 1:
        polarity, (source_req, excerpt) = next(iter(hints.items()))
        return polarity, source_req, excerpt
    return None, None, None


def _unit_decline(requirement: Requirement, probe: str) -> str | None:
    """Why this row's unit cannot be judged by ``probe``, or ``None`` when it can."""
    limits = requirement.limits
    unit = "" if limits is None else str(limits.unit or "").strip()
    expected = _probe_units(probe)
    if not expected:
        return None
    if not unit:
        return (
            f"the extracted limit declares no unit, and the {probe} probe judges a number in "
            f"{expected}; binding it would compare different quantities"
        )
    base, _scale = normalize_unit(unit)
    if base != expected:
        return (
            f"the {probe} probe measures a value in {expected}, but this row declares "
            f"{unit!r}; binding it would judge the wrong number"
        )
    return None


def bind_requirements(
    requirements: Sequence[Requirement], *, unverified: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], ...]:
    """Deterministically bind requirement rows to probe ids (or declare them untestable).

    ``unverified`` maps requirement ids whose citation could not be verified to
    the observed reason; those rows are never bound, because a model must not be
    credited with a datasheet row nobody could confirm is on the cited page.

    The result is a tuple of binding entries in requirement order, one per row:
    ``{"req_id", "probe", "params"}`` for a bound row and
    ``{"req_id", "probe": None, "not_testable_reason"}`` otherwise. The same
    input always produces the same output, so a run is reproducible from
    ``bindings.json``.
    """
    blocked = dict(unverified or {})
    entries: list[dict[str, Any]] = []
    io_context = any(
        _rule_matches(rule, _search_text(row)) for row in requirements for rule in _IO_RULES
    )
    for requirement in requirements:
        req_id = requirement.req_id
        if requirement.req_class in (RequirementClass.UNKNOWN, RequirementClass.ABSOLUTE_MAXIMUM):
            entries.append(
                {
                    "req_id": req_id,
                    "probe": None,
                    "not_testable_reason": "unknown classification or absolute stress rating; not an operating model target",
                }
            )
            continue
        if req_id in blocked:
            entries.append(
                {
                    "req_id": req_id,
                    "probe": None,
                    "not_testable_reason": f"citation_unverified: {blocked[req_id]}",
                }
            )
            continue
        if not _has_limits(requirement):
            entries.append(
                {
                    "req_id": req_id,
                    "probe": None,
                    "not_testable_reason": (
                        "the extracted requirement declares no numeric limit, and this harness "
                        "judges one measured number per characteristic"
                    ),
                }
            )
            continue
        text = _search_text(requirement)
        statement = _negative_text(_normalized(requirement.statement))
        decline: str | None = None
        for rule in (*_RULES, *_IO_RULES) if io_context else _RULES:
            if not _rule_matches(rule, text):
                continue
            if rule.statement_none_of and any(
                _mentions(statement, phrase) for phrase in rule.statement_none_of
            ):
                continue
            if rule.probe is None:
                decline = f"{rule.name}: {rule.reason}"
                break
            mismatch = _unit_decline(requirement, rule.probe)
            if mismatch is not None:
                decline = f"{rule.name}: {mismatch}"
                break
            from boardmodeler.authoring.conditions import operating_params

            seed = None
            provenance = None
            if rule.probe.startswith("io_"):
                polarity, source_req, source_excerpt = _cited_polarity(
                    requirement, requirements, blocked
                )
                if polarity is not None:
                    seed = {"io_inverting": polarity}
                    provenance = {
                        "io_inverting": {
                            "source_req": source_req,
                            "excerpt": source_excerpt,
                            "reason": (
                                "output polarity compiled from a verified citation for this "
                                "part and document"
                            ),
                        }
                    }
            params, problem = operating_params(requirement, PROBES[rule.probe], seed=seed)
            if problem:
                decline = problem
                break
            entry: dict[str, Any] = {"req_id": req_id, "probe": rule.probe, "params": params}
            if provenance is not None:
                entry["derived_conditions"] = provenance
            entries.append(entry)
            break
        else:
            decline = (
                f"no deterministic probe measures this statement ({_subject(requirement)}); it "
                "stays a declared gap rather than being stretched onto the nearest probe"
            )
        if decline is not None:
            entries.append({"req_id": req_id, "probe": None, "not_testable_reason": decline})
    return tuple(entries)


# --------------------------------------------------------------------------- #
# backends


def build_backend(request: MakeModelRequest) -> AuthorBackend:
    """The backend ``request.backend_name`` names, or one that says why not.

    ``api`` (the default) speaks the configured provider's documented HTTP shape
    with an API key — ``request.provider`` picks the provider id and
    ``request.agent_model`` its model, both falling back to the persisted
    settings. ``bob`` is the Bob-only edition's CLI; a general build refuses that
    name with a reason instead of constructing anything. ``scripted``/``fixture``
    name the offline author: it writes the bundled behavioural regulator template
    (and a symbol for
    it) when ``request.subckt`` names one, and writes nothing otherwise — the
    harness then reports the missing model with its own reason. It is what the
    GUI's integration runs and anyone without an agent key use; it never pretends
    to have authored a model it did not write. An unknown name is refused by name
    (never substituted). The offline tests override this function to inject their
    own scripted backend.
    """
    name = str(request.backend_name or "").strip().lower()
    if name in ("", "api"):
        if not internet_allowed():
            # The API author is the backend that reaches the provider's API, so the
            # product's single switch is checked before anything is constructed: no
            # request is built, and the reason reaches the caller as BLOCKED.
            return UnavailableBackend("api", refusal_detail("authoring a model over the API"))
        # ``turn_timeout_s`` bounds one agent invocation; when the caller leaves it
        # unset the API-key path applies its own finite 600 s budget, retries included.
        limit = float(request.turn_timeout_s) if request.turn_timeout_s else DEFAULT_API_TIMEOUT_S
        return build_api_backend(
            provider_id=request.provider,
            model=request.agent_model,
            max_tokens=request.agent_max_tokens,
            team_id=request.team_id,
            timeout_s=limit,
        )
    if name == "bob":
        if not BOB_ONLY:
            return UnavailableBackend(
                "bob",
                "bob_backend_unavailable: the general edition does not include Bob Shell; "
                "use the 'api' backend with a vendor API key",
            )
        if not internet_allowed():
            # The Bob CLI also answers over the network, so the same switch governs it.
            return UnavailableBackend("bob", refusal_detail("authoring a model with Bob"))
        return BobShellBackend(team_id=request.team_id, timeout_s=request.turn_timeout_s)
    if name in ("scripted", "fixture"):
        return _bundled_author(request)
    known = (
        "'api', 'bob', 'scripted' or 'fixture'" if BOB_ONLY else "'api', 'scripted' or 'fixture'"
    )
    return UnavailableBackend(
        name or "unknown",
        f"{name or 'unknown'}_backend_unavailable: unknown backend name; use {known}",
    )


def _bundled_author(request: MakeModelRequest) -> ScriptedBackend:
    """The offline author: the bundled template for the requested subcircuit."""
    from boardmodeler.models.regulator import write_regulator_library

    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = Path(workdir) / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        try:
            lib = write_regulator_library(model_dir / f"{request.subckt}.lib", [request.subckt])
        except KeyError:
            return  # no bundled template declares this subcircuit: write nothing
        ports = list(subckt_ports(lib.read_text(encoding="utf-8"), request.subckt))
        model_dir.joinpath(f"{request.subckt}.asy").write_text(
            symbol_text(request.subckt, ports, model_file=f"{request.subckt}.lib"),
            encoding="utf-8",
            newline="\n",
        )

    return ScriptedBackend(script, name="scripted")


# --------------------------------------------------------------------------- #
# internal control flow


class _Stop(Exception):
    """An expected failure inside one run, converted to a status by :func:`make_model`."""

    def __init__(self, stage: str, status: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.status = status
        self.detail = detail


class _StageLog:
    """Every emitted :class:`StageEvent`, in order, plus the caller's callback."""

    def __init__(self, callback: Callable[[StageEvent], None] | None) -> None:
        self._callback = callback
        self.events: list[StageEvent] = []

    def emit(
        self,
        stage: str,
        status: str,
        detail: str,
        counts: Mapping[str, int] | None = None,
    ) -> StageEvent:
        event = StageEvent(stage=stage, status=status, detail=detail, counts=dict(counts or {}))
        self.events.append(event)
        if self._callback is not None:
            self._callback(event)
        return event


class _Ledger:
    """Wall-clock seconds per stage, from an injectable monotonic clock.

    The record answers "where did the time go" for one build. ``author`` includes the
    model-writing agent turns and every LTspice check made inside them. The run adds
    the backend's observed inference attempts to this record before saving it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self.seconds: dict[str, float] = {}

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        began = self._clock()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + (self._clock() - began)

    def payload(self, **fields: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "record_kind": "run_timing",
            **fields,
            "stage_seconds": {name: round(value, 3) for name, value in self.seconds.items()},
            "total_seconds": round(self._clock() - self._started, 3),
        }


def _checked(request: MakeModelRequest) -> MakeModelRequest:
    if not isinstance(request, MakeModelRequest):
        raise TypeError(f"request must be a MakeModelRequest, got {type(request).__name__}")
    if not str(request.part).strip():
        raise ValueError("part must be a non-empty part number")
    if not _SUBCKT_RE.match(str(request.subckt)):
        raise ValueError(
            f"subckt {request.subckt!r} is not a sanitized SPICE identifier "
            "(expected [A-Za-z_][A-Za-z0-9_]*)"
        )
    if request.verification not in ("full", "sanity"):
        raise ValueError("verification must be full or sanity")
    if request.engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {request.engine!r}")
    if request.family is not None:
        from boardmodeler.models.support import family_ids

        if request.family not in family_ids():
            raise ValueError(f"family must be one of {family_ids()}, got {request.family!r}")
    if request.engine == "pin_only" and request.verification != "full":
        raise ValueError("the pin_only route judges with LTspice and needs verification=full")
    if request.engine == "behavioral" and request.verification != "full":
        raise ValueError("the behavioral route judges with LTspice and needs verification=full")
    if request.max_iterations is not None and request.max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1 or None, got {request.max_iterations}")
    if request.stall_patience < 1:
        raise ValueError(f"stall_patience must be >= 1, got {request.stall_patience}")
    if request.timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {request.timeout_s}")
    if request.turn_timeout_s is not None and request.turn_timeout_s <= 0:
        raise ValueError(f"turn_timeout_s must be > 0 or None, got {request.turn_timeout_s}")
    if request.agent_max_tokens is not None and request.agent_max_tokens < 1:
        raise ValueError(f"agent_max_tokens must be >= 1 or None, got {request.agent_max_tokens}")
    return request


def _first_page_text(path: Path, limit: int = 1500) -> str:
    """The head of the datasheet first page: it usually says what kind of part this is."""
    try:
        document = read_pdf(path, max_pages=1)
    except Exception:  # an unreadable file is the read stage business, not the family
        return ""
    if not document.pages:
        return ""
    return " ".join(document.pages[0].text.split())[:limit]


def _page_count(path: Path) -> int | None:
    """Pages in ``path``, or ``None`` when it cannot be read as a PDF."""
    try:
        return len(read_pdf(path, max_pages=0).pages) or None
    except Exception:
        return None


def _read_requirements_file(
    path: Path,
) -> tuple[dict[str, Any], list[Requirement]]:
    """``(declared document, rows)`` from a supplied extraction result."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _Stop("read", Status.BLOCKED.value, f"requirements_unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _Stop(
            "read", Status.BLOCKED.value, f"requirements_unreadable: {path} is not JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        raise _Stop(
            "read",
            Status.BLOCKED.value,
            f"requirements_unreadable: {path} has no 'requirements' list",
        )
    declared = payload.get("document")
    declared = declared if isinstance(declared, dict) else {}
    try:
        requirements = [Requirement.model_validate(item) for item in payload["requirements"]]
    except Exception as exc:
        raise _Stop(
            "read",
            Status.BLOCKED.value,
            f"requirements_invalid: {path} does not match the requirement schema: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return declared, requirements


def _page_lookup(record: DocumentRecord, store: DocumentStore):
    """Page text for the registered document, read once and served from memory.

    Citation verification visits every cited page; reading the PDF once (the same
    eager read ``DocumentStore.add_file`` already performs) costs one pass, where
    growing the page count per citation costs one pass *per page*.
    """
    document = None
    second: list[Any] = []  # the pdfium reading, read once and only when the file is a PDF
    state: dict[str, str] = {}

    def lookup(doc_id: str, pdf_page: int) -> str | None:
        nonlocal document
        if doc_id != record.doc_id:
            state["reason"] = f"unknown document {doc_id!r}"
            return None
        try:
            path = store.original_path(doc_id)
        except (KeyError, DocumentStoreError, OSError, ValueError) as exc:
            state["reason"] = f"the stored original could not be opened ({type(exc).__name__})"
            return None
        if path.suffix.lower() in _TEXT_SUFFIXES:
            if pdf_page != 0:
                state["reason"] = "a text document has only page 0"
                return None
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                state["reason"] = f"the stored text could not be read ({type(exc).__name__})"
                return None
        if document is None:
            try:
                document = read_pdf(path)
            except Exception as exc:
                # Keep the cause: this used to return a bare ``None``, so a failure to
                # read the datasheet surfaced later as an AttributeError inside the
                # reviewed-row extractor instead of as the read failure it is.
                state["reason"] = f"the PDF could not be read ({type(exc).__name__}: {exc})"
                return None
        if pdf_page >= len(document.pages):
            state["reason"] = (
                f"page {pdf_page + 1} is beyond this document's {len(document.pages)} pages"
            )
            return None
        text = page_text(document, pdf_page)
        if not text.strip():
            state["reason"] = (
                f"page {pdf_page + 1} has no extractable text (an image-only page needs OCR)"
            )
            return text
        if not second:
            try:
                second.append(read_pdf_pdfium(path))
            except Exception:  # one reading is still a reading; the second is only a tolerance
                second.append(None)
        other = second[0]
        if other is not None and pdf_page < len(other.pages):
            return text + READING_BREAK + other.pages[pdf_page].text
        return text

    #: Why the last lookup returned nothing usable: cited-page refusals must name it.
    lookup.last_reason = lambda: state.get("reason", "")  # type: ignore[attr-defined]
    return lookup


#: The reviewed LM358 rows are cited from one page of the TI datasheet (table 5.7).
_LM358_REFERENCE_PAGE = 9


def _required_text(characteristic: object) -> str:
    """The human limit text for one row, e.g. ``min 4 / max 4.5 V``."""
    unit = getattr(characteristic, "unit", "") or ""
    parts: list[str] = []
    for label, value in (
        ("min", getattr(characteristic, "min_value", None)),
        ("max", getattr(characteristic, "max_value", None)),
    ):
        if value is not None:
            parts.append(f"{label} {value:g} {unit}".strip())
    if parts:
        return " / ".join(parts)
    typ = getattr(characteristic, "typ_value", None)
    if typ is not None:
        return f"typ {typ:g} {unit} (+/-10 %)".strip()
    return "no numeric limit"


# --------------------------------------------------------------------------- #
# the run


#: Reasons that mean this run could not use the model at all, so a rejection quoted in one
#: of them is about the model. A timeout is deliberately absent: it is inconclusive, not
#: proof of an invalid model. These are the reasons the pipeline actually produces
#: (``validate_library``, ``model_ports``, and the harness run diagnostics).
_HARD_FAILURE_PREFIXES = (
    "model_syntax_invalid",
    "model_lib_unreadable",
    "sim_output_unreadable",
    "sim_convergence_failure",
)


def _rejection_from_report(report: HarnessReport) -> str | None:
    """The simulator's own rejection recorded in a run, if the run recorded one."""
    from boardmodeler.authoring.sanity import rejection_marker

    for outcome in report.outcomes:
        reason = (outcome.unknown_reason or "").strip()
        while reason.startswith("deferred_after_invalid_simulation: "):
            reason = reason.removeprefix("deferred_after_invalid_simulation: ").strip()
        if not reason.startswith(_HARD_FAILURE_PREFIXES):
            continue
        rejected = rejection_marker(reason)
        if rejected is not None:
            return rejected
    return None


class _Run:
    """One make-model run: the six stages, the state they produce, the result."""

    def __init__(self, request: MakeModelRequest, log: _StageLog) -> None:
        self.request = request
        self.log = log
        from boardmodeler.storage import local_path

        self.out_dir = local_path(request.out_dir)
        self.workdir = self.out_dir / WORK_DIRNAME
        self.cache_dir = self.out_dir / CACHE_DIRNAME
        self.spec_dir = self.out_dir / SPEC_DIRNAME
        self.record: DocumentRecord | None = None
        self.store: DocumentStore | None = None
        self.supplied: list[Requirement] = []
        self.declared: dict[str, Any] = {}
        self.requirements: list[Requirement] = []
        self.pin_map: tuple[dict[str, Any], ...] = ()
        self.unverified: dict[str, str] = {}
        self.spec: SpecSet | None = None
        self.outcome: BuildOutcome | None = None
        self.report = HarnessReport(part=request.part, model_sha256="", spec_digest="", outcomes=())
        self.reinforcement: ReinforcementReport | None = None
        self.backend: AuthorBackend | None = None
        self._backend_counter_start = 0
        self.backend_name = ""
        self.turns = 0
        self.sanity_ok = False
        self.reference_bindings: list[dict[str, Any]] | None = None
        self.reviewed_extraction: dict[str, Any] | None = None
        self.citation_lookup = None
        self.status = Status.UNKNOWN.value
        self.detail = ""
        self.lib_path: Path | None = None
        self.asy_path: Path | None = None
        self.card_path: Path | None = None
        self.files: list[Path] = []
        self.template_seed: dict[str, Any] | None = None
        self.template_seed_bytes: bytes | None = None
        self.template_design: Any = None
        self.pin_only_info: dict[str, Any] | None = None
        self.template_name = "buck_template"
        self.template_heading = "Buck template"
        self.support: Any = None
        self.head_text = ""
        self.template_seed_judge_s: float | None = None
        self.template_compile_s: float | None = None
        self.qualification_plan: Any = None
        self.qualification_problem: str | None = None
        self.qualification_report: dict[str, Any] | None = None
        self.previous_publication_dir: Path | None = None
        self.publication_problem: str | None = None
        self.pinout_contract: Any = None
        self.pinout_report: dict[str, Any] | None = None

    def _get_backend(self) -> AuthorBackend:
        """Keep one backend so extraction, planning and authoring share one meter."""
        if self.backend is None:
            self.backend = build_backend(self.request)
            if isinstance(self.backend, _API_BACKEND_TYPES):
                self._backend_counter_start = self.backend.provider_calls
            elif isinstance(self.backend, BobShellBackend):
                self._backend_counter_start = self.backend.shell_invocations
        return self.backend

    def _provider_call_fields(self) -> dict[str, Any]:
        """Count HTTP attempts exactly; never guess requests hidden inside a CLI."""
        backend = self.backend
        fields: dict[str, Any] = {
            "provider_calls_definition": "inference HTTP attempts handed to transport",
            "provider_calls_observed": 0,
            "provider_calls_complete": True,
            "provider_calls": 0,
        }
        if isinstance(backend, _API_BACKEND_TYPES):
            calls = backend.provider_calls - self._backend_counter_start
            fields["provider_calls_observed"] = calls
            fields["provider_calls"] = calls
        elif isinstance(backend, BobShellBackend):
            invocations = backend.shell_invocations - self._backend_counter_start
            fields["backend_invocations"] = invocations
            if invocations:
                fields["provider_calls"] = None
                fields["provider_calls_complete"] = False
                fields["provider_calls_unknown_reason"] = (
                    "Bob Shell does not report its internal provider requests"
                )
        elif backend is not None and not isinstance(backend, (ScriptedBackend, UnavailableBackend)):
            fields["provider_calls"] = None
            fields["provider_calls_complete"] = False
            fields["provider_calls_unknown_reason"] = (
                f"{type(backend).__name__} does not expose request attempts"
            )
        return fields

    # ------------------------------------------------------------------ stages

    def read(self) -> None:
        request = self.request
        datasheet = Path(request.datasheet)
        self.log.emit("read", "running", f"registering {datasheet.name!r} as a document")
        if not datasheet.is_file():
            raise _Stop(
                "read",
                Status.BLOCKED.value,
                f"datasheet_missing: {datasheet} does not exist; pick the datasheet PDF and re-run",
            )
        declared: dict[str, Any] = {}
        supplied: list[Requirement] = []
        if request.requirements_json is not None:
            declared, supplied = _read_requirements_file(Path(request.requirements_json))
            from boardmodeler.domain.records import PinDefinition

            try:
                saved = json.loads(Path(request.requirements_json).read_text(encoding="utf-8"))
                saved_pins = saved.get("pin_map", [])
                # Our exact reviewed LM358 snapshot intentionally records only sourced
                # names/numbers. Replaying it must not invent electrical pin semantics.
                from boardmodeler.models.pinout import resolve_reviewed_profile

                minimal_reviewed = False
                if saved_pins and all(
                    isinstance(pin, dict) and set(pin) <= {"name", "physical_pin"}
                    for pin in saved_pins
                ):
                    try:
                        profile = resolve_reviewed_profile(
                            request.part, hashlib.sha256(datasheet.read_bytes()).hexdigest()
                        )
                        minimal_reviewed = profile.profile_id == "ti-lm358-pinout-v1"
                    except ValueError:
                        pass
                self.pin_map = tuple(
                    dict(pin)
                    if minimal_reviewed
                    else PinDefinition.model_validate(pin).model_dump(mode="json")
                    for pin in saved_pins
                )
            except (ValueError, TypeError) as exc:
                raise _Stop("read", "BLOCKED", f"saved pin map is invalid: {exc}") from exc
        doc_id, note = self._document_id(declared, datasheet)
        store = DocumentStore(self.workdir)
        try:
            self.record = store.add_file(
                datasheet,
                doc_type="datasheet",
                provenance="user_supplied",
                classification="unknown",
                remote_inference_allowed=bool(request.allow_remote),
                doc_id=doc_id,
                update_remote_permission=True,
            )
        except Exception as exc:
            raise _Stop(
                "read",
                Status.BLOCKED.value,
                f"datasheet_unreadable: {datasheet} could not be registered as a document "
                f"({type(exc).__name__}: {exc})",
            ) from exc
        self.head_text = _first_page_text(store.original_path(self.record.doc_id))
        # The refusal sits here because this is the first point where both the part
        # number and the document's own text are in hand, and it is long before the
        # extraction, the agent and the simulator: a part the probes cannot judge is
        # stopped without a turn being spent. The stage's own writes (the registered
        # document) have already happened; the refusal adds none, and save() publishes
        # nothing for a run that never reached the authoring stage.
        refusal = classify(request.part, text=self.record.title)
        if not refusal.supported:
            raise _Stop("read", Status.BLOCKED.value, refusal.detail)
        from boardmodeler.models.support import decide_support

        identity = decide_support(request.part, title=self.record.title, head=self.head_text)
        if identity.state == "blocked_class":
            raise _Stop("read", Status.BLOCKED.value, identity.refusal(request.engine))
        self.store = store
        self.supplied = supplied
        self.declared = declared
        detail = (
            f"{self.record.doc_id}: {self.record.page_count} page(s), "
            f"{self.record.text_extraction} text"
        )
        if note:
            detail = f"{detail}; {note}"
        self.log.emit("read", "ok", detail, {"pages": int(self.record.page_count)})

    def _document_id(self, declared: Mapping[str, Any], datasheet: Path) -> tuple[str | None, str]:
        """Which doc id to register the picked PDF under.

        An extraction result that names its document is re-verified against the
        picked PDF, but only when the PDF can be that document (same page count);
        otherwise the citations would be checked against a different file and
        every row would be reported unverified for the wrong reason.
        """
        doc_id = str(declared.get("doc_id") or "").strip()
        if not doc_id:
            return None, ""
        pages = declared.get("page_count")
        if pages is None:
            return doc_id, ""
        actual = _page_count(datasheet)
        if actual is not None and actual != int(pages):
            return None, (
                f"the supplied datasheet has {actual} page(s) while the extraction cites "
                f"{pages}; citations were not re-verified against it"
            )
        return doc_id, ""

    def preflight(self) -> None:
        """Refuse impossible new builds before extraction or AI test planning."""
        if (
            self.request.requirements_json is not None
            or self.request.backend_name in ("fixture", "scripted")
            or self.record is None
        ):
            return
        from boardmodeler.authoring import tps54331_reference
        from boardmodeler.models.pinout import PinoutError, resolve_reviewed_profile
        from boardmodeler.models.support import decide_support

        decision = decide_support(
            self.request.part,
            title=self.record.title,
            head=self.head_text,
            declared_family=self.request.family,
        )
        if decision.state == "blocked_class":
            self.support = decision
            raise _Stop("gate", Status.BLOCKED.value, decision.refusal(self.request.engine))
        if self.request.part.strip().upper() == "UCC28251":
            raise _Stop(
                "pinout",
                Status.BLOCKED.value,
                "package_required: UCC28251 has different PW (TSSOP) and RGP (QFN) pin maps; "
                "choose the package before building (UCC28251PW or UCC28251RGP)",
            )
        # This exact reviewed reader produces a cheap, useful diagnostic of the
        # remaining evidence/package gaps, without calling a provider.
        if tps54331_reference.matches(self.request.part, self.record.file_hash):
            return
        try:
            resolve_reviewed_profile(self.request.part, self.record.file_hash)
        except PinoutError as exc:
            raise _Stop(
                "pinout",
                Status.BLOCKED.value,
                "unsupported_part: source-backed package pinout is not reviewed for this part "
                "and datasheet revision; extraction was not sent to AI because the current "
                f"engine could not publish its result ({exc})",
            ) from exc

    def extract(self, cancel: threading.Event | None) -> None:
        if self.request.requirements_json is not None:
            self.requirements = self.supplied
            validation = validate_requirements(self.requirements, documents=self._documents())
            if validation.errors:
                raise _Stop(
                    "extract",
                    Status.BLOCKED.value,
                    "requirements_invalid: "
                    + "; ".join(
                        f"{issue.code}: {issue.message}" for issue in validation.errors[:3]
                    ),
                )
            self._verify_citations()
            counts = self._row_counts()
            self.log.emit(
                "extract",
                "skipped",
                f"using the supplied extraction result {Path(self.request.requirements_json).name} "
                f"({len(self.requirements)} rows validated, "
                f"{counts['unverified']} citation(s) unverified)",
                counts,
            )
            return

        if self.record is not None and self.request.backend_name not in ("fixture", "scripted"):
            from boardmodeler.authoring import ucc28251_reference

            if ucc28251_reference.matches(self.request.part, self.record.file_hash):
                self.citation_lookup = _page_lookup(self.record, self.store)
                pages = {
                    page: self.citation_lookup(self.record.doc_id, page) or ""
                    for page in ucc28251_reference.PAGES
                }
                try:
                    (
                        self.requirements,
                        self.reference_bindings,
                        self.pin_map,
                        self.reviewed_extraction,
                    ) = ucc28251_reference.records(self.record, pages, part=self.request.part)
                except ValueError as exc:
                    raise _Stop("extract", Status.BLOCKED.value, str(exc)) from exc
                validation = validate_requirements(self.requirements, documents=self._documents())
                if validation.errors:
                    raise _Stop(
                        "extract",
                        Status.BLOCKED.value,
                        "reviewed_extraction_invalid: "
                        + "; ".join(issue.message for issue in validation.errors[:3]),
                    )
                self._verify_citations()
                if self.unverified:
                    raise _Stop(
                        "extract", Status.BLOCKED.value, "reviewed extraction citations failed"
                    )
                self._write_json(
                    self.out_dir / "reviewed-extraction.json", self.reviewed_extraction
                )
                self.log.emit(
                    "extract",
                    "ok",
                    "partial reviewed UCC28251 Rev. E rows and selected package matched the exact TI datasheet; zero extraction API calls",
                    self._row_counts(),
                )
                return
            from boardmodeler.authoring import tps54331_reference

            if tps54331_reference.matches(self.request.part, self.record.file_hash):
                self.citation_lookup = _page_lookup(self.record, self.store)
                pages = {
                    page: self.citation_lookup(self.record.doc_id, page) or ""
                    for page in tps54331_reference.PAGES
                }
                try:
                    self.requirements, self.pin_map, self.reviewed_extraction = (
                        tps54331_reference.records(self.record, pages)
                    )
                except ValueError as exc:
                    raise _Stop("extract", Status.BLOCKED.value, str(exc)) from exc
                validation = validate_requirements(self.requirements, documents=self._documents())
                if validation.errors:
                    raise _Stop(
                        "extract",
                        Status.BLOCKED.value,
                        "reviewed_extraction_invalid: "
                        + "; ".join(issue.message for issue in validation.errors[:3]),
                    )
                self._verify_citations()
                if self.unverified:
                    raise _Stop(
                        "extract", Status.BLOCKED.value, "reviewed extraction citations failed"
                    )
                self._write_json(
                    self.out_dir / "reviewed-extraction.json", self.reviewed_extraction
                )
                self.log.emit(
                    "extract",
                    "ok",
                    "reviewed TPS54331 SLVS839H table columns matched the exact TI datasheet; "
                    "partial extraction; D/DDA package unresolved; zero extraction API calls",
                    self._row_counts(),
                )
                return
            from boardmodeler.authoring.lm358_reference import matches, records

            if matches(self.request.part, self.record.file_hash):
                self.citation_lookup = _page_lookup(self.record, self.store)
                page = self.citation_lookup(self.record.doc_id, _LM358_REFERENCE_PAGE)
                if not page or not page.strip():
                    # The reviewed rows are cited on one page. If that page cannot be
                    # read, the honest outcome is a named refusal: the extractor used
                    # to be handed ``None`` and died with an AttributeError, which read
                    # like a crash instead of "this datasheet's page is unreadable".
                    raise _Stop(
                        "extract",
                        Status.BLOCKED.value,
                        "datasheet_page_unreadable: the reviewed rows for this part are cited "
                        f"at page {_LM358_REFERENCE_PAGE} of this datasheet and that page "
                        f"yielded no usable text ({self.citation_lookup.last_reason()}); "
                        "install OCR or supply --requirements with the reviewed rows instead",
                    )
                self.requirements, self.reference_bindings, self.pin_map = records(
                    self.record, page
                )
                self._verify_citations()
                if self.unverified:
                    raise _Stop(
                        "extract", Status.BLOCKED.value, "reviewed extraction citations failed"
                    )
                self.log.emit(
                    "extract",
                    "ok",
                    "reviewed LM358 table 5.7 and eight-pin map matched the exact TI datasheet; zero extraction API calls",
                    self._row_counts(),
                )
                return
        self.log.emit("extract", "running", "asking the extraction provider for the datasheet rows")
        from boardmodeler.pipeline.project import ProjectError, create_project
        from boardmodeler.requirements.extract import extract_requirements

        config = load_config()
        try:
            if self.request.backend_name in ("fixture", "scripted"):
                extraction_provider = select_provider(
                    config,
                    requested="fixture",
                    allow_bob_shell=False,
                    fixture_dir=self.cache_dir,
                ).provider
            else:
                self._get_backend()
                extraction_provider = AgentExtractionProvider(
                    self.backend,
                    part=self.request.part,
                    diagnostics_dir=self.workdir / "evidence" / "extraction-attempts",
                    progress=lambda detail: self.log.emit("extract", "running", detail),
                )
        except ProviderError as exc:
            raise _Stop(
                "extract",
                Status.BLOCKED.value,
                f"{exc.code}: {exc.detail}; configure a provider or supply requirements_json",
            ) from exc
        try:
            project = create_project(
                self.workdir,
                project_id=f"{self.request.subckt.lower()}_model",
                name=f"{self.request.part} model",
                notes=[f"created by make_model for {self.request.part}"],
            )
        except (ProjectError, OSError, ValueError) as exc:
            raise _Stop(
                "extract",
                Status.BLOCKED.value,
                f"workspace_unusable: {self.workdir} could not be prepared: {exc}",
            ) from exc
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        policy = config.data_policy
        # --allow-remote / the disclosed GUI GO action authorizes this chosen file,
        # without falsely declaring an unclassified document to be public.
        if self.request.allow_remote:
            policy = policy.model_copy(
                update={
                    "deny_unknown_classification": False,
                    "permitted_classifications": [*policy.permitted_classifications, "unknown"],
                }
            )
        try:
            result = extract_requirements(
                project,
                provider=extraction_provider,
                document_ids=[self.record.doc_id],
                cache_dir=self.cache_dir,
                policy=policy,
                allow_remote=True if self.request.allow_remote else None,
                cancel=cancel,
            )
        except ProviderError as exc:
            raise _Stop("extract", Status.BLOCKED.value, f"{exc.code}: {exc.detail}") from exc
        except Exception as exc:
            raise _Stop(
                "extract",
                Status.BLOCKED.value,
                f"provider_error: {type(exc).__name__}: {exc}",
            ) from exc
        self.requirements = apply_review(result.requirements, result.review)
        self.pin_map = tuple(pin.model_dump(mode="json") for pin in result.pins)
        errors = [issue for issue in result.issues if issue.severity == "error"]
        if errors:
            raise _Stop(
                "extract",
                Status.BLOCKED.value,
                "requirements_invalid: "
                + "; ".join(f"{issue.code}: {issue.message}" for issue in errors[:3]),
            )
        self._verify_citations()
        counts = self._row_counts()
        counts["cache_hits"] = int(result.cache_hits)
        self.log.emit(
            "extract",
            "ok",
            f"{result.detail}; {counts['unverified']} citation(s) unverified",
            counts,
        )

    def _documents(self) -> dict[str, DocumentRecord]:
        """The registered document, when this run's rows actually cite it.

        A supplied extraction result may name a document the picked PDF cannot be
        (see :meth:`_document_id`). There is then nothing in this run to check the
        citations against, and returning the record anyway would mark every row
        unverified for the wrong reason.
        """
        if self.record is None:
            return {}
        cited = {
            reference.doc_id
            for requirement in self.requirements
            for reference in requirement.evidence
        }
        if cited and self.record.doc_id not in cited:
            return {}
        return {self.record.doc_id: self.record}

    def _verify_citations(self) -> None:
        """Record this run's citation checks on the rows and in :attr:`unverified`.

        With the cited document registered and readable, the excerpts are checked
        against its own page text. Without it, a supplied extraction result cannot
        verify its own citations; the document must be available. Persist the new
        result on DOCUMENT rows before any frozen source or qualification plan is
        written, so a replay's old ``citation_verified`` flag cannot certify itself.
        """
        documents = self._documents()
        if documents:
            assert self.record is not None
            checks = verify_citations(
                self.requirements,
                documents,
                excerpt_lookup=self.citation_lookup or _page_lookup(self.record, self.store),
            )
            self.unverified = {
                requirement.req_id: (
                    "the excerpt is not on the page it cites, or the citation is incomplete "
                    f"(doc {self.record.doc_id})"
                )
                for requirement in self.requirements
                if requirement.origin is RequirementOrigin.DOCUMENT
                and checks.get(requirement.req_id) is not True
            }
        else:
            self.unverified = {
                requirement.req_id: (
                    "the datasheet text for the cited document is not available, so the citation "
                    "could not be verified"
                )
                for requirement in self.requirements
                if requirement.origin is RequirementOrigin.DOCUMENT
            }
        self.requirements = [
            requirement.model_copy(
                update={"citation_verified": requirement.req_id not in self.unverified}
            )
            if requirement.origin is RequirementOrigin.DOCUMENT
            else requirement
            for requirement in self.requirements
        ]

    def _row_counts(self) -> dict[str, int]:
        return {"rows": len(self.requirements), "unverified": len(self.unverified)}

    def bind(self, cancel=None) -> None:
        request = self.request
        self.log.emit("bind", "running", "binding datasheet rows to the probe registry")
        self.spec_dir.mkdir(parents=True, exist_ok=True)
        requirements_path = self._write_requirements()
        if request.verification == "sanity":
            from boardmodeler.authoring.sanity import DEFERRED

            entries = [
                {"req_id": r.req_id, "probe": None, "not_testable_reason": DEFERRED}
                for r in self.requirements
            ]
            note = "quick mode: AI test planning and full simulation deferred"
        elif request.bindings_json is not None:
            supplied = Path(request.bindings_json)
            if not supplied.is_file():
                raise _Stop(
                    "bind",
                    Status.BLOCKED.value,
                    f"bindings_missing: {supplied} does not exist; drop bindings_json to "
                    "regenerate the binding from the requirements",
                )
            entries = self._supplied_entries(supplied)
            note = f"binding file supplied by the caller ({supplied.name})"
        elif self.reference_bindings is not None:
            entries = self.reference_bindings
            note = "reviewed LM358 operating points and dual-amplifier probes"
        elif (
            self.pin_map
            and self.request.backend_name not in ("fixture", "scripted")
            and (self.request.engine == "legacy_ai" or self.request.plan_tests)
        ):
            # the agent plans the extra test circuits only on the agent route; the local
            # routes bind with the reviewed keyword table and never ask a provider
            from boardmodeler.authoring.test_planner import plan_bindings

            try:
                entries = plan_bindings(
                    self.requirements,
                    self.pin_map,
                    self._get_backend(),
                    self.workdir / "evidence" / "test-plans",
                    part=self.request.part,
                    unverified=self.unverified,
                    cancel=cancel,
                    progress=lambda detail: self.log.emit("bind", "running", detail),
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise _Stop("bind", Status.BLOCKED.value, str(exc)) from exc
            note = "independent device-specific fixtures, frozen before model authoring"
        else:
            entries = bind_requirements(self.requirements, unverified=self.unverified)
            note = "binding computed from the reviewed keyword table"
        if (
            request.verification == "full"
            and request.bindings_json is None
            and self.reference_bindings is None
        ):
            from boardmodeler.authoring.buck_fixtures import complete_buck_bindings

            entries = complete_buck_bindings(
                self.requirements, self.pin_map, entries, unverified=self.unverified
            )
        # The binding this run is judged against is always written for review, and
        # *it* is what the loader reads, so ``bindings_json`` replays a run exactly.
        bindings_path = self._write_json(
            self.spec_dir / BINDINGS_NAME,
            {
                "part": request.part,
                "subckt": request.subckt,
                "doc_id": "" if self.record is None else self.record.doc_id,
                "note": note if request.verification == "sanity" else _BINDING_NOTE,
                "bindings": [dict(entry) for entry in entries],
            },
        )
        try:
            self.spec = load_tps54320_spec(
                requirements_path, bindings_path, part=request.part, subckt=request.subckt
            )
        except (OSError, ValueError) as exc:
            raise _Stop(
                "bind",
                Status.BLOCKED.value,
                f"spec_invalid: {exc}; fix requirements_json/bindings_json and re-run",
            ) from exc
        self._write_json(self.spec_dir / CHARACTERISTICS_NAME, json.loads(self.spec.to_json()))
        counts = {
            "testable": len(self.spec.covered()),
            "not_testable": len(self.spec.uncovered()),
        }
        self.log.emit(
            "bind",
            "ok",
            (
                f"{len(self.requirements)} datasheet rows retained; no test-planning requests"
                if request.verification == "sanity"
                else f"{counts['testable']} row(s) judged by probes, "
                f"{counts['not_testable']} declared not testable; {note}"
            ),
            counts,
        )

    def _supplied_entries(self, path: Path) -> list[dict[str, Any]]:
        """The binding entries of a supplied ``bindings_json`` file, shape-checked."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise _Stop("bind", Status.BLOCKED.value, f"bindings_unreadable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise _Stop(
                "bind", Status.BLOCKED.value, f"bindings_unreadable: {path} is not JSON: {exc}"
            ) from exc
        entries = payload.get("bindings") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise _Stop(
                "bind",
                Status.BLOCKED.value,
                f"bindings_unreadable: {path} has no 'bindings' list; regenerate it by running "
                "without bindings_json",
            )
        return [dict(entry) for entry in entries]

    def _write_requirements(self) -> Path:
        """The requirement set this run is judged against, in the loader's fixture shape."""
        record = self.record
        document: dict[str, Any] = {"doc_id": "" if record is None else record.doc_id}
        if self.reviewed_extraction is not None:
            document["reviewed_extraction"] = self.reviewed_extraction
        if record is not None:
            document.update(
                {
                    "title": record.title,
                    "page_count": record.page_count,
                    "text_extraction": record.text_extraction,
                    "file_hash": record.file_hash,
                }
            )
        path = self.spec_dir / REQUIREMENTS_NAME
        self._write_json(
            path,
            {
                "document": document,
                "requirements": [
                    json.loads(requirement.model_dump_json(by_alias=True))
                    for requirement in self.requirements
                ],
                "pin_map": list(self.pin_map),
            },
        )
        return path

    def _write_json(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    # ------------------------------------------------------------ author/judge

    def _seed_template(
        self,
        request: BuildRequest,
        path: Path,
        cancel: threading.Event | None,
        impl: Any = None,
    ) -> HarnessReport | None:
        """Judge a deterministic first candidate before spending an author turn.

        A seed is only a starting model. Its values are traced to cited rows or
        named defaults, while only the simulator may award measured PASS rows.
        Existing models are left for the ordinary revalidation path. ``impl`` is the
        registered implementation to seed from; None means the buck template, which is
        what the agent route has always tried first.
        """
        if path.is_file() or self.spec is None:
            return None
        from boardmodeler.authoring.harness import run_harness
        from boardmodeler.authoring.validation_cache import validation_key, write_report
        from boardmodeler.models.support import IMPLEMENTATIONS

        if impl is None:
            impl = next(item for item in IMPLEMENTATIONS if item.name == "peak_current_buck")
        label = impl.template.replace("_", " ")
        compile_started = time.monotonic()
        try:
            seed = impl.seed(self.spec, self.unverified)
        except ValueError as exc:
            self.log.emit("author", "skipped", f"{label} unavailable: {exc}")
            return None
        if seed is None:
            return None
        self.template_name, self.template_heading = impl.template, impl.heading
        seed.write(path)
        self.template_compile_s = time.monotonic() - compile_started
        self.template_seed = seed.payload()
        self.template_seed_bytes = path.read_bytes()
        self.template_design = seed.design
        self.log.emit("author", "running", f"judging a cited {label} before agent repair")
        cache_root = self.workdir / "validation-cache"
        key = validation_key(path, self.spec, request.ltspice, request.timeout_s)
        run_dir = (
            cache_root / key if key is not None else self.workdir / "harness" / "template-seed"
        )
        judge_started = time.monotonic()
        try:
            report = run_harness(
                model_lib=path,
                subckt=request.subckt,
                spec=self.spec,
                workdir=run_dir,
                ltspice=request.ltspice,
                timeout_s=request.timeout_s,
                cancel=cancel,
            )
        except Exception as exc:
            self.log.emit(
                "judge", "failed", f"{label} simulation failed: {type(exc).__name__}: {exc}"
            )
            return None
        self.template_seed_judge_s = time.monotonic() - judge_started
        write_report(cache_root, key, report)
        self._on_report(report)
        return report

    def support_gate(self) -> None:
        """Refuse before any generation when the requested route may not run for this part.

        The decision is saved whether or not it allows the route: a refusal, a limited route
        and a supported claim are all facts the deliverables should carry.
        """
        from boardmodeler.models.support import RouteVerdict, decide_support

        title = "" if self.record is None else self.record.title
        decision = decide_support(
            self.request.part,
            title=title,
            head=self.head_text,
            spec=self.spec,
            unverified=self.unverified,
            declared_family=self.request.family,
        )
        # Common pins do not resolve package-specific exposed pads. The marker is
        # frozen into requirements/bindings replays, so a later numeric/test fix
        # cannot turn an ambiguous pin map into a published D or DDA model.
        if decision.state not in ("blocked_class", "unclassified") and any(
            pin.get("package_resolution") == "unresolved" for pin in self.pin_map
        ):
            from boardmodeler.authoring.tps54331_reference import PACKAGE_GAP

            reason = f"{decision.reason}; missing {PACKAGE_GAP}"
            refusal = f"unsupported_part: unresolved_package: {reason}"
            decision = dataclasses.replace(
                decision,
                state="unsupported_family",
                reason=reason,
                missing=(*decision.missing, PACKAGE_GAP),
                routes={name: RouteVerdict(False, refusal) for name in decision.routes},
            )
        self.support = decision
        route = self.request.engine
        record = {
            "schema_version": 1,
            "record_kind": "support_decision",
            "part": self.request.part,
            "engine": route,
            "state": decision.state,
            "family": decision.family,
            "identified_from": decision.identified_from,
            "implementation": decision.implementation,
            "reason": decision.reason,
            "missing": list(decision.missing),
            "routes": {
                name: {"allowed": verdict.allowed, "reason": verdict.reason}
                for name, verdict in decision.routes.items()
            },
            "note": (
                "Support means represented, cited and independently tested. It is not a pass: "
                "verdicts come from the LTspice rows."
            ),
        }
        with contextlib.suppress(OSError):
            text = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False)
            self._write_text(self.out_dir / SUPPORT_RECORD_NAME, text + chr(10))
        if not decision.allows(route):
            raise _Stop("author", Status.BLOCKED.value, decision.refusal(route))

    def _pinout_source_hash(self) -> str:
        from boardmodeler.models.pinout import PinoutError

        if self.record is None:
            raise PinoutError("pinout_source_unavailable: no registered datasheet")
        path = (
            self.store.original_path(self.record.doc_id)
            if self.store is not None
            else Path(self.request.datasheet)
        )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != self.record.file_hash:
            raise PinoutError("pinout_source_changed: registered document bytes changed")
        return actual

    def _save_pinout_report(self) -> None:
        if self.pinout_report is not None:
            self._write_text(
                self.out_dir / PINOUT_REPORT_NAME,
                json.dumps(self.pinout_report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            )

    def freeze_pinout(self) -> None:
        """Confirm package evidence before spending an author or simulator turn."""
        from boardmodeler.models.pinout import (
            freeze_pinout,
            pinout_report,
            resolve_reviewed_profile,
        )

        source_hash = "" if self.record is None else self.record.file_hash
        try:
            if self.spec is None:
                raise ValueError("pinout_spec_missing")
            source_hash = self._pinout_source_hash()
            profile = resolve_reviewed_profile(self.request.part, source_hash)
            if self.pin_map != self.spec.pin_map:
                raise ValueError("pinout_live_map_changed: extracted and frozen maps differ")
            self.pinout_contract = freeze_pinout(profile, self.spec, source_hash)
            self._write_text(self.spec_dir / PINOUT_CONTRACT_NAME, self.pinout_contract.to_json())
            self.pinout_report = pinout_report(
                part=self.request.part,
                document_sha256=source_hash,
                spec_digest=self.spec.digest(),
                contract=self.pinout_contract,
            )
            self._save_pinout_report()
        except (OSError, ValueError, TypeError) as exc:
            self.pinout_report = pinout_report(
                part=self.request.part,
                document_sha256=source_hash,
                spec_digest="" if self.spec is None else self.spec.digest(),
                contract=self.pinout_contract,
                reason=str(exc),
            )
            raise _Stop("gate", Status.BLOCKED.value, f"pinout_not_confirmed: {exc}") from exc
        self.log.emit(
            "gate", "ok", "source-confirmed package pinout frozen; publication check pending"
        )

    def _check_pinout_publication(self, library: bytes, symbol: bytes, model_file: str) -> None:
        """Check the current source/map and the exact staged output bytes, on every route."""
        from boardmodeler.models.pinout import (
            check_publication,
            pinout_report,
            resolve_reviewed_profile,
        )

        source_hash = "" if self.record is None else self.record.file_hash
        try:
            if self.spec is None or self.pinout_contract is None:
                raise ValueError("pinout_confirmation_missing: no frozen pinout contract")
            if self.pin_map != self.spec.pin_map:
                raise ValueError("pinout_live_map_changed: extracted and frozen maps differ")
            saved = self.spec_dir / PINOUT_CONTRACT_NAME
            if (
                not saved.is_file()
                or json.loads(saved.read_text(encoding="utf-8")) != self.pinout_contract.payload()
            ):
                raise ValueError("pinout_contract_changed: saved contract differs")
            source_hash = self._pinout_source_hash()
            approved = resolve_reviewed_profile(self.request.part, source_hash)
            if approved != self.pinout_contract.profile:
                raise ValueError(
                    "pinout_contract_profile_changed: application-owned approval differs"
                )
            self.pinout_report = check_publication(
                self.pinout_contract,
                spec=self.spec,
                document_sha256=source_hash,
                library=library,
                symbol=symbol,
                model_file=model_file,
            )
        except (OSError, ValueError, TypeError) as exc:
            self.pinout_report = pinout_report(
                part=self.request.part,
                document_sha256=source_hash,
                spec_digest="" if self.spec is None else self.spec.digest(),
                contract=self.pinout_contract,
                reason=str(exc),
                library=library,
                symbol=symbol,
            )
        self._save_pinout_report()
        if not self.pinout_report["publication_allowed"]:
            raise ValueError(f"pinout_not_confirmed: {self.pinout_report['reason']}")

    def _author_behavioral(self, cancel: threading.Event | None) -> None:
        """Judge a code-built candidate with the LTspice harness: no agent turn, no fallback."""
        install = locate()
        if install is None:
            self.log.emit("author", "failed", LTSPICE_MISSING)
            self.status, self.detail = Status.BLOCKED.value, LTSPICE_MISSING
            return
        prepare_workdir(spec=self.spec, subckt=self.request.subckt, workdir=self.workdir)
        request = BuildRequest(
            part=self.request.part,
            subckt=self.request.subckt,
            spec=self.spec,
            workdir=self.workdir,
            ltspice=install.path,
            backend=UnavailableBackend("behavioral", "no agent runs on the behavioral route"),
            max_iterations=self.request.max_iterations,
            stall_patience=self.request.stall_patience,
            turn_timeout_s=self.request.turn_timeout_s,
            timeout_s=self.request.timeout_s,
        )
        path = model_file(self.workdir, self.request.subckt)
        path.unlink(missing_ok=True)  # a code-built route regenerates; it never reuses a model
        from boardmodeler.models.support import IMPLEMENTATIONS

        name = None if self.support is None else self.support.implementation
        impl = next((item for item in IMPLEMENTATIONS if item.name == name), None)
        seed_report = (
            None
            if impl is None or impl.seed is None
            else self._seed_template(request, path, cancel, impl)
        )
        if seed_report is None:
            detail = (
                "behavioral_route_no_candidate: the matched implementation produced no "
                "candidate; nothing was authored"
            )
            self.log.emit("author", "failed", detail)
            self.status, self.detail = Status.BLOCKED.value, detail
            return
        passed = seed_report.passed()
        self.outcome = BuildOutcome(
            status=Status.PASS.value if passed else Status.UNKNOWN.value,
            iterations=0,
            report=seed_report,
            history=("code-built candidate judged by the LTspice harness; no agent turn",),
            detail=(
                "every bound simulator row passed; zero agent turns"
                if passed
                else "code-built candidate judged; failing and unknown rows remain open; "
                "no agent repair on this route"
            ),
        )
        self.report = seed_report
        self.log.emit("author", "ok", "code-built candidate judged; zero agent turns", {"turns": 0})

    def _author_pin_only(self, cancel: threading.Event | None) -> None:
        """Build the limited pin-only model from the pin table and judge it with the gate.

        No function is modelled and no datasheet row is judged, so the result is never a
        pass; a model the viability gate finds unusable is not delivered.
        """
        from boardmodeler.authoring.viability import GatePin, GateSpec, run_gate
        from boardmodeler.models.pin_only import PinOnlyRefusal, build_pin_only
        from boardmodeler.models.pin_shell import level_param
        from boardmodeler.models.symbolism import symbol_text

        try:
            built = build_pin_only(self.request.part, self.request.subckt, self.pin_map)
        except PinOnlyRefusal as exc:
            raise _Stop("author", Status.BLOCKED.value, str(exc)) from exc
        install = locate()
        if install is None:
            self.log.emit("author", "failed", LTSPICE_MISSING)
            self.status, self.detail = Status.BLOCKED.value, LTSPICE_MISSING
            return
        prepare_workdir(spec=self.spec, subckt=self.request.subckt, workdir=self.workdir)
        path = model_file(self.workdir, self.request.subckt)
        path.write_text(built.library_text, encoding="utf-8", newline="\n")
        asy = path.with_suffix(".asy")
        asy.write_text(
            symbol_text(self.request.subckt, list(built.ports), model_file=path.name),
            encoding="utf-8",
        )
        claimed = built.claimed_alarms()
        gate_spec = GateSpec(
            self.request.part,
            self.request.subckt,
            tuple(
                GatePin(
                    pin.port,
                    pin.kind,
                    pin.number,
                    alarms=claimed.get(pin.port, ()),
                    open_drain=pin.topology == "open_drain",
                    force=level_param(pin.port) if pin.kind in ("output", "io") else None,
                )
                for pin in built.pins
            ),
        )
        self.log.emit("author", "running", "judging the pin-only model with the viability gate")
        started = time.monotonic()
        gate = run_gate(path, gate_spec, install.path, self.workdir / "pin-only-gate", asy_path=asy)
        seconds = time.monotonic() - started
        failing = [c for c in gate.checks if c.status in (Status.FAIL, Status.BLOCKED)]
        unknown = [c for c in gate.checks if c.status is Status.UNKNOWN]
        passed = len(gate.checks) - len(failing) - len(unknown)
        self.pin_only_info = {
            "schema_version": 1,
            "record_kind": "pin_only_report",
            "part": self.request.part,
            "rail": built.rail,
            "ground": built.ground,
            "ports": list(built.ports),
            "notes": list(built.notes),
            "gate_status": gate.status.value,
            "gate_benches": gate.runs,
            "gate_seconds": round(seconds, 2),
            "gate_checks": [check.as_dict() for check in gate.checks],
            "verdict": "UNJUDGED",
            "verdict_note": "Pins, supply draw, clamps and alarms only; no function and no "
            "datasheet row was judged.",
        }
        self.report = HarnessReport(
            part=self.request.part,
            model_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            spec_digest=self.spec.digest(),
            outcomes=(),
        )
        if failing:
            names = ", ".join(check.id for check in failing)
            detail = (
                f"pin_only_model_not_viable: the viability gate failed {names}; nothing was "
                "delivered and no other route was used instead"
            )
            path.unlink(missing_ok=True)
            status = Status.BLOCKED.value
        else:
            status = Status.UNKNOWN.value
            detail = (
                "pin_only: pins, supply draw, clamps and alarms only; no function is modelled "
                "and no datasheet row was judged, so this is never a pass. Viability gate: "
                f"{passed} checks pass, {len(unknown)} unknown, 0 fail ({gate.runs} benches, "
                f"{seconds:.0f} s). Confirm the pin table against the datasheet pinout."
            )
        self.outcome = BuildOutcome(
            status=status,
            iterations=0,
            report=self.report,
            history=("pin-only model built from the pin table and judged by the viability gate",),
            detail=detail,
        )
        self.log.emit("author", "ok" if not failing else "failed", detail[:160], {"turns": 0})

    def author(self, cancel: threading.Event | None) -> None:
        if self.request.engine == "pin_only":
            self._author_pin_only(cancel)
            return
        if self.request.engine == "behavioral":
            self._author_behavioral(cancel)
            return
        self.log.emit("author", "running", f"checking the {self.request.backend_name!r} backend")
        backend = self._get_backend()
        self.backend_name = backend.name
        if self.request.verification == "sanity":
            from boardmodeler.authoring.sanity import LABEL, author_model

            if not _BUILD_LOCK.acquire(blocking=False):
                raise _Stop("author", "BLOCKED", "another model build is running")
            try:
                prepare_workdir(
                    spec=self.spec,
                    subckt=self.request.subckt,
                    workdir=self.workdir,
                    prompt="Quick structural checks; full simulation was not requested.",
                )
                install = None
                try:
                    install = locate()
                except OSError as exc:
                    # Quick mode must never become harder to run than it was without a
                    # simulator: a probing failure leaves the load check unavailable.
                    self.log.emit("author", "running", f"LTspice was not located: {exc}")
                checked, turns = author_model(
                    self.spec,
                    backend,
                    self.workdir,
                    cancel,
                    lambda detail: self.log.emit("author", "running", detail),
                    self.unverified,
                    max_attempts=min(2, self.request.max_iterations or 2),
                    ltspice=install.path if install is not None else None,
                )
                self.report = HarnessReport(
                    part=self.request.part,
                    model_sha256=checked["model_sha256"],
                    spec_digest=self.spec.digest(),
                    outcomes=(),
                )
                self.sanity_ok = True
                self.status, self.detail = "UNKNOWN", LABEL
                load = checked.get("load_check") or {}
                self.log.emit("author", "ok", f"model ready after {turns} author turn(s)")
                self.log.emit(
                    "judge",
                    "ok",
                    f"{LABEL}; LTspice load: {load.get('status', 'not checked')} "
                    f"({load.get('detail', 'no bounded load check ran')})",
                )
            except Exception as exc:
                self.status, self.detail = "UNKNOWN", f"sanity authoring stopped: {exc}"
                self.log.emit("judge", "failed", self.detail)
            finally:
                _BUILD_LOCK.release()
            return
        install = locate()
        if install is None:
            self.log.emit("author", "failed", LTSPICE_MISSING)
            self.status, self.detail = Status.BLOCKED.value, LTSPICE_MISSING
            return
        if self.spec is None:  # pragma: no cover - bind() always sets it or stops
            self.status, self.detail = Status.UNKNOWN.value, "spec_missing: no specification"
            return
        prepare_workdir(spec=self.spec, subckt=self.request.subckt, workdir=self.workdir)
        if not self.spec.covered():
            detail = (
                f"no_covered_characteristics: none of {len(self.spec.uncovered())} row(s) is "
                "reachable by a probe; no model was authored or simulated"
            )
            self.log.emit("author", "skipped", detail)
            self.status, self.detail = Status.UNKNOWN.value, detail
            return
        from boardmodeler.authoring.validation_cache import (
            progress_score,
            read_report,
            validation_key,
        )

        request = BuildRequest(
            part=self.request.part,
            subckt=self.request.subckt,
            spec=self.spec,
            workdir=self.workdir,
            ltspice=install.path,
            backend=backend,
            max_iterations=self.request.max_iterations,
            stall_patience=self.request.stall_patience,
            turn_timeout_s=self.request.turn_timeout_s,
            timeout_s=self.request.timeout_s,
        )
        path = model_file(self.workdir, self.request.subckt)
        from boardmodeler.authoring.model_reference import normalize_ground_reference

        normalize_ground_reference(
            path, self.spec.pin_map, self.workdir / "evidence" / "ground-reference", spec=self.spec
        )
        seed_report = self._seed_template(request, path, cancel)
        if seed_report is not None and seed_report.passed():
            self.outcome = BuildOutcome(
                status=Status.PASS.value,
                iterations=0,
                report=seed_report,
                history=("cited buck template passed the LTspice harness; zero agent turns",),
                detail="template passed every bound simulator row; zero agent turns",
            )
            self.report = seed_report
            self.log.emit("author", "ok", "template passed; zero agent turns", {"turns": 0})
            return
        key = validation_key(path, self.spec, install.path, self.request.timeout_s)
        cached = read_report(self.workdir / "validation-cache", key, self.spec, path)
        prechecked = seed_report is not None or (
            cached is None and not (cancel and cancel.is_set())
        )
        if prechecked and seed_report is None:
            # A fresh process has no receipt for an existing candidate. Re-judge it with
            # one LTspice run before any remote reinforcement or author turn is spent.
            revalidated = revalidate_candidate(request, cancel)
            if revalidated is not None:
                self.outcome = revalidated
                self.report = revalidated.report
                self.log.emit(
                    "author",
                    "ok",
                    "existing candidate revalidated by the simulator; zero author turns",
                    {"turns": 0},
                )
                self.log.emit("judge", "ok", "reused revalidated simulator evidence")
                return
        if cached is None or not cached.passed():
            if _author_needs_network(self.request) and not internet_allowed():
                usable, reason = False, refusal_detail("authoring a model through the provider")
            else:
                usable, reason = backend.availability()
            if not usable:
                if seed_report is not None and any(
                    row.status in (Status.PASS.value, Status.FAIL.value) and row.artifacts
                    for row in seed_report.outcomes
                ):
                    self.outcome = BuildOutcome(
                        status=Status.UNKNOWN.value,
                        iterations=0,
                        report=seed_report,
                        history=("template simulated; agent repair unavailable",),
                        detail=f"template model measured with unresolved rows; agent repair: {reason}",
                    )
                    self.report = seed_report
                    self.log.emit(
                        "author", "ok", "template delivered with measured limits", {"turns": 0}
                    )
                    return
                self.log.emit("author", "failed", reason)
                self.status, self.detail = Status.BLOCKED.value, reason
                return
            if seed_report is None or self.request.reinforce is True:
                self._gather_supporting_material(cancel)
            else:
                self.log.emit(
                    "reinforce", "skipped", "template already supplies the repair context"
                )
            request = dataclasses.replace(
                request,
                supporting_context="\n".join(
                    f"{source.url} (SHA256 {source.sha256}): {source.excerpt}"
                    for source in (self.reinforcement.sources if self.reinforcement else ())
                    if source.retrieved and source.sha256 and source.excerpt
                ),
            )
        if seed_report is not None:
            request = dataclasses.replace(
                request,
                max_iterations=1,
                turn_timeout_s=min(request.turn_timeout_s or 150.0, 150.0),
            )
        self.log.emit(
            "judge",
            "running",
            "the harness runs after every agent turn; no turn has finished yet",
        )
        if not _BUILD_LOCK.acquire(blocking=False):
            reason = (
                "build_in_progress: another model build is running in this process; wait for it "
                "to finish and re-run"
            )
            self.log.emit("author", "failed", reason)
            self.status, self.detail = Status.BLOCKED.value, reason
            return
        try:
            outcome = build_model(
                request, cancel, candidate_revalidated=prechecked, on_report=self._on_report
            )
        finally:
            _BUILD_LOCK.release()
        if seed_report is not None and self.template_seed_bytes is not None:
            better = bool(outcome.report.outcomes) and progress_score(
                outcome.report, self.spec
            ) < progress_score(seed_report, self.spec)
            if not better and outcome.status != Status.PASS.value:
                path.write_bytes(self.template_seed_bytes)
                outcome = BuildOutcome(
                    status=Status.UNKNOWN.value,
                    iterations=outcome.iterations,
                    report=seed_report,
                    history=(*outcome.history, "restored the measured template seed"),
                    detail=f"template retained after bounded repair; {outcome.detail}",
                )
        self.outcome = outcome
        self.report = outcome.report
        model_written = model_file(self.workdir, self.request.subckt).is_file()
        self.log.emit(
            "author",
            "ok" if model_written else "failed",
            f"{outcome.iterations} turn(s); {outcome.detail}",
            {"turns": int(outcome.iterations)},
        )
        if self.turns == 0 and outcome.report.passed():
            self.log.emit("judge", "ok", "reused matching, hash-verified simulator evidence")
        elif self.turns == 0:
            self.log.emit("judge", "skipped", f"no harness turn ran: {outcome.detail}")

    def _gather_supporting_material(self, cancel: threading.Event | None) -> None:
        """One bounded search for supporting material, recorded but never a verdict.

        Errata, application notes and vendor-model caveats about the part can change how a
        reader interprets a model, so they are gathered here and listed on the card. They
        cannot change a status: only the frozen datasheet rows judge the model. The stage
        never fails the build — disabled, unreachable, cancelled and empty results are all
        reported as such, and the run continues. ``cancel`` is the build's event; it and
        ``reinforce_timeout_s`` bound only this search, never the author loop.
        """
        # Explicit --reinforce never overrides the one SETUP Internet switch.
        try:
            require_network("the supporting-material search")
        except NetworkRefused as refused:
            self.log.emit("reinforce", "skipped", refused.detail[:160])
            return
        enabled = self.request.reinforce is not False
        digest = self.spec.digest() if self.spec is not None else ""
        try:
            report = reinforce(
                part=self.request.part,
                spec_digest=digest,
                out_dir=self.workdir,
                backend=self.backend,
                enabled=bool(enabled),
                timeout_s=self.request.reinforce_timeout_s,
                cancel=cancel,
            )
        except Exception as exc:  # pragma: no cover - the stage must never break a build
            self.log.emit("reinforce", "skipped", f"reinforcement unavailable: {exc}"[:160])
            return
        self.reinforcement = report
        if report.status == "ok":
            retrieved = sum(1 for source in report.sources if source.retrieved)
            unverified = len(report.sources) - retrieved
            detail = (
                f"{retrieved} source(s) retrieved, {unverified} unverified claim(s), "
                f"{len(report.caveats)} caveat(s), {len(report.suggested_probes)} suggested probe(s)"
            )
        else:
            detail = report.detail
        self.log.emit("reinforce", "ok" if report.status == "ok" else "skipped", detail[:160])

    def _on_report(self, report: HarnessReport) -> None:
        """One completed harness turn: report it, with the harness's own counts."""
        self.report = report
        self.turns += 1
        counts = dict(report.counts())
        counts["turn"] = self.turns
        failing = ", ".join(outcome.probe_id for outcome in report.failing()) or "none"
        self.log.emit(
            "judge",
            "ok" if report.outcomes else "failed",
            f"turn {self.turns}: {counts['PASS']} pass, {counts['FAIL']} fail, "
            f"{counts['UNKNOWN']} unknown; failing: {failing}",
            counts,
        )

    # ------------------------------------------------------------------- save

    def _archive_deliverables(self, *, include_diagnostics: bool = False) -> None:
        """Withdraw only app-owned root outputs, retaining their exact old bytes.

        The workdir candidate and measured evidence remain available for revalidation.
        Old root files are history, never the deliverables of a refused rerun.
        """
        names = {
            f"{self.request.subckt}.lib",
            f"{self.request.subckt}.asy",
            "MODEL_CARD.md",
            EXAMPLE_NAME,
            "example.cir",
            "install.md",
            HARNESS_REPORT_NAME,
            DESIGN_RECORD_NAME,
            "template-parameters.json",
            "sanity-report.json",
            "pin-only-report.json",
            QUALIFICATION_REPORT_NAME,
            RESULTS_NAME,
            TIMING_NAME,
        }
        if include_diagnostics:
            names.update(
                (
                    SUPPORT_RECORD_NAME,
                    "reviewed-extraction.json",
                    PINOUT_REPORT_NAME,
                    "official-model.json",
                )
            )
        # A reused output directory can switch subcircuit names. Only recorded
        # direct-child libraries/symbols count; a results file cannot name outsiders.
        try:
            previous = json.loads((self.out_dir / RESULTS_NAME).read_text(encoding="utf-8"))
            for key, suffix in (("lib_path", ".lib"), ("asy_path", ".asy")):
                value = previous.get(key)
                if isinstance(value, str):
                    path = Path(value)
                    if (
                        path.parent.resolve() == self.out_dir.resolve()
                        and path.suffix.lower() == suffix
                    ):
                        names.add(path.name)
        except OSError, ValueError, AttributeError:
            pass
        history = self.workdir / "publication-history" / uuid.uuid4().hex
        moved = False
        seen: set[str] = set()
        for name in sorted(names):
            path = self.out_dir / name
            # Windows case aliases refer to one artifact. The existence check
            # also keeps the code correct on case-sensitive platforms.
            identity = str(path.absolute())
            if identity in seen or not path.is_file():
                continue
            seen.add(identity)
            history.mkdir(parents=True, exist_ok=True)
            path.rename(history / name)
            moved = True
        if include_diagnostics or (
            self.qualification_plan is None and self.status == Status.BLOCKED.value
        ):
            prior_plan = self.spec_dir / QUALIFICATION_PLAN_NAME
            if prior_plan.is_file():
                (history / SPEC_DIRNAME).mkdir(parents=True, exist_ok=True)
                prior_plan.rename(history / SPEC_DIRNAME / QUALIFICATION_PLAN_NAME)
                moved = True
        if include_diagnostics or (
            self.pinout_contract is None and self.status == Status.BLOCKED.value
        ):
            prior_pinout = self.spec_dir / PINOUT_CONTRACT_NAME
            if prior_pinout.is_file():
                (history / SPEC_DIRNAME).mkdir(parents=True, exist_ok=True)
                prior_pinout.rename(history / SPEC_DIRNAME / PINOUT_CONTRACT_NAME)
                moved = True
        if moved and self.previous_publication_dir is None:
            self.previous_publication_dir = history
        self.lib_path = self.asy_path = self.card_path = None

    def save(self) -> None:
        self.log.emit("save", "running", f"publishing deliverables into {self.out_dir}")
        notes: list[str] = []
        if self.pinout_report is None:
            from boardmodeler.models.pinout import pinout_report

            self.pinout_report = pinout_report(
                part=self.request.part,
                document_sha256="" if self.record is None else self.record.file_hash,
                spec_digest="" if self.spec is None else self.spec.digest(),
                reason="pinout_not_checked: this build did not reach source confirmation",
            )
        try:
            self._archive_deliverables()
        except OSError as exc:
            self.publication_problem = f"publication_withdrawal_failed: {exc}"
            self.detail = f"{self.detail}; {self.publication_problem}".strip("; ")
            self.log.emit("save", "failed", self.publication_problem, {"files": 0})
            return
        gate_report_saved = False
        if self.pin_only_info is not None:
            # Keep the observed gate result even when a failed gate withholds the library.
            try:
                self._write_text(
                    self.out_dir / "pin-only-report.json",
                    json.dumps(self.pin_only_info, indent=2, sort_keys=True, ensure_ascii=False)
                    + "\n",
                )
            except OSError as exc:
                notes.append(
                    f"pin-only gate report could not be saved: {type(exc).__name__}: {exc}"
                )
            else:
                gate_report_saved = True
                notes.append("saved the pin-only gate report")
        source = None if self.spec is None else model_file(self.workdir, self.request.subckt)
        if self.status == Status.BLOCKED.value or (
            self.outcome is not None and self.outcome.status == Status.BLOCKED.value
        ):
            source = None
        if self.request.verification == "sanity" and not self.sanity_ok:
            source = None
        if self.template_seed is not None and not any(
            row.status in (Status.PASS.value, Status.FAIL.value) and row.artifacts
            for row in self.report.outcomes
        ):
            source = None
            self.detail = (
                f"{self.detail}; template_not_simulated: no measured LTspice artifact was produced"
            ).strip("; ")
        if source is not None and source.is_file():
            try:
                self._publish(source, notes)
            except (OSError, ValueError, ModelStoreError) as exc:
                notes.append(f"the model could not be published: {type(exc).__name__}: {exc}")
                refusal = f"model_not_published: {exc}"
                self.publication_problem = refusal
                if self.pinout_report is not None:
                    self.pinout_report.update(
                        status="BLOCKED", publication_allowed=False, reason=refusal
                    )
                prior_detail = self.detail or (self.outcome.detail if self.outcome else "")
                self.detail = f"{prior_detail}; {refusal}".strip("; ")
                try:
                    self._archive_deliverables()
                except OSError as cleanup:
                    self.detail += f"; publication_withdrawal_failed: {cleanup}"
                self.lib_path = self.asy_path = self.card_path = None
        else:
            remaining = (
                f"{SPEC_DIRNAME}/, pin-only-report.json and {RESULTS_NAME}"
                if gate_report_saved
                else f"{SPEC_DIRNAME}/ and {RESULTS_NAME}"
            )
            notes.append(f"no model file was written, so {remaining} describe this run")
        published = [path for path in (self.lib_path, self.asy_path, self.card_path) if path]
        try:
            self._save_pinout_report()
        except OSError as exc:
            notes.append(f"pinout report unavailable: {exc}")
        self.log.emit(
            "save",
            "ok" if self.lib_path is not None else "skipped",
            "; ".join(notes) or f"published {len(published)} model file(s)",
            {"files": len(published)},
        )

    def freeze_qualification(self) -> None:
        """Freeze independent buck checks before any author can write the candidate."""
        with contextlib.suppress(OSError):
            (self.spec_dir / QUALIFICATION_PLAN_NAME).unlink(missing_ok=True)
        if (
            self.request.verification != "full"
            or self.spec is None
            or self.support is None
            or self.support.implementation != "peak_current_buck"
        ):
            return
        from boardmodeler.authoring.qualification import build_buck_qualification_plan

        try:
            plan = build_buck_qualification_plan(self.spec, self.spec_dir / REQUIREMENTS_NAME)
            self._write_text(self.spec_dir / QUALIFICATION_PLAN_NAME, plan.to_json())
        except (OSError, ValueError, TypeError) as exc:
            self.qualification_problem = f"qualification_plan_unavailable: {exc}"
            self.log.emit("qualify", "skipped", self.qualification_problem)
            return
        self.qualification_plan = plan
        self.log.emit("qualify", "ready", "fixed buck conditions and mandatory checklist frozen")

    def qualify(self, cancel: threading.Event | None) -> None:
        """Judge only the exact published library; keep unfinished checks visible."""
        if self.lib_path is None or (
            self.qualification_plan is None and self.qualification_problem is None
        ):
            return
        from boardmodeler.authoring.qualification import run_qualification

        if self.qualification_plan is None:
            report: dict[str, Any] = {
                "record_kind": "qualification_report",
                "part": self.request.part,
                "status": "UNKNOWN",
                "family_qualified": False,
                "reason": self.qualification_problem,
                "model_sha256": hashlib.sha256(self.lib_path.read_bytes()).hexdigest(),
            }
        else:
            try:
                install = locate()
                design = json.loads((self.out_dir / DESIGN_RECORD_NAME).read_text(encoding="utf-8"))
                digest = (
                    design.get("design_sha256")
                    if self._saved_design_is_exact(design, self.lib_path.read_bytes())
                    else None
                )
                report = run_qualification(
                    self.qualification_plan,
                    self.lib_path,
                    None if install is None else install.path,
                    self.workdir / "qualification",
                    design_sha256=digest,
                    timeout_s=min(self.request.timeout_s, 120.0),
                    cancel=cancel,
                )
            except (OSError, ValueError, TypeError) as exc:
                report = {
                    "record_kind": "qualification_report",
                    "part": self.request.part,
                    "status": "UNKNOWN",
                    "family_qualified": False,
                    "reason": f"qualification_unavailable: {type(exc).__name__}: {exc}",
                    "model_sha256": hashlib.sha256(self.lib_path.read_bytes()).hexdigest(),
                }
        self.qualification_report = report
        try:
            self._write_json(self.out_dir / QUALIFICATION_REPORT_NAME, report)
        except OSError as exc:
            self.log.emit("qualify", "failed", f"qualification report could not be saved: {exc}")
            return
        if self.card_path is not None:
            counts = report.get("counts") or {}
            summary = (
                ", ".join(
                    f"{key} {counts.get(key, 0)}" for key in ("PASS", "FAIL", "UNKNOWN", "BLOCKED")
                )
                if counts
                else report.get("reason", "unavailable")
            )
            try:
                self._write_text(
                    self.card_path,
                    self.card_path.read_text(encoding="utf-8")
                    + "\n## Independent buck qualification\n\n"
                    + f"{report['status']} ({summary}). "
                    + "This is a supplemental, fixed checklist; a passing subset does not "
                    + "qualify the whole family. See `qualification-report.json` and "
                    + "`spec/qualification-plan.json` for the model hash, conditions, "
                    + "measurements and explicit gaps.\n",
                )
            except OSError as exc:
                self.log.emit(
                    "qualify", "incomplete", f"qualification card note unavailable: {exc}"
                )
        self.log.emit(
            "qualify",
            "ok" if report.get("family_qualified") else "incomplete",
            f"fixed qualification {report['status']}; family qualified: "
            f"{bool(report.get('family_qualified'))}",
        )

    def _receipt_load(self) -> dict:
        """The bounded load result this build recorded, for the model card."""
        try:
            payload = json.loads((self.workdir / "sanity-report.json").read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {"status": "not checked", "detail": "no sanity receipt was written"}
        load = payload.get("load_check")
        return load if isinstance(load, dict) else {"status": "not checked"}

    def _refuse_known_invalid(self, text: str) -> None:
        """Refuse to publish a model that is already known to be invalid.

        Two independent signals count, because either alone would let an unusable
        library reach the user: the deterministic syntax rules, and the simulator's own
        rejection recorded in this run's outcomes. The artifacts stay in the build
        folder for diagnosis; only the deliverable is withheld.
        """
        from boardmodeler.authoring.model_syntax import validate_library
        from boardmodeler.authoring.probes import ProbeError

        staged = self.workdir / "publish-check" / f"{self.request.subckt}.lib"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(text, encoding="utf-8", newline="\n")
        try:
            validate_library(staged)
        except ProbeError as exc:
            raise ValueError(f"model_syntax_rejected: {exc.full_reason()}") from exc
        rejected = _rejection_from_report(self.report)
        if rejected is not None:
            raise ValueError(f"simulator_rejected_model: {rejected}")

    def _publish(self, source: Path, notes: list[str]) -> None:
        request = self.request
        delivered = source.read_bytes()
        text = delivered.decode("utf-8")
        self._refuse_known_invalid(text)
        if self.status == Status.BLOCKED.value or (
            self.outcome is not None and self.outcome.status == Status.BLOCKED.value
        ):
            raise ValueError("publication_blocked: this run did not authorize a candidate")
        if self.spec is None or self.report.spec_digest != self.spec.digest():
            raise ValueError("publication_spec_mismatch: report does not describe the frozen spec")
        if hashlib.sha256(delivered).hexdigest() != self.report.model_sha256:
            raise ValueError("publication_model_mismatch: candidate differs from the judged bytes")
        for frozen in (
            self.spec_dir / CHARACTERISTICS_NAME,
            self.workdir / SPEC_DIRNAME / CHARACTERISTICS_NAME,
        ):
            if (
                frozen.is_file()
                and SpecSet.from_json(frozen.read_text(encoding="utf-8")).digest()
                != self.report.spec_digest
            ):
                raise ValueError("publication_spec_mismatch: frozen spec file differs from report")
        ports = list(subckt_ports(text, request.subckt))
        if not ports:
            raise ValueError(f"{source} declares no .subckt {request.subckt}")
        if source.read_bytes() != delivered:
            raise ValueError("publication_model_mismatch: candidate changed during publication")
        lib_target = self.out_dir / f"{request.subckt}.lib"
        staged_symbol, note = self._publish_symbol(
            ports,
            lib_target.name,
            destination=self.workdir / "publish-check" / f"{request.subckt}.asy",
        )
        symbol_bytes = staged_symbol.read_bytes()
        self._check_pinout_publication(delivered, symbol_bytes, lib_target.name)
        if source.read_bytes() != delivered or staged_symbol.read_bytes() != symbol_bytes:
            raise ValueError(
                "publication_pinout_artifact_changed: staged bytes changed after confirmation"
            )
        lib_target.parent.mkdir(parents=True, exist_ok=True)
        lib_target.write_bytes(delivered)
        self.lib_path = lib_target
        symbol = self.out_dir / f"{request.subckt}.asy"
        symbol.write_bytes(symbol_bytes)
        self.asy_path = symbol
        self._publish_design_record(lib_target.read_bytes(), symbol=symbol, ports=ports)
        notes.append(note)
        written = write_deliverables(
            out_dir=self.out_dir,
            part=request.part,
            subckt=request.subckt,
            spec=self.spec,
            report=self.report,
            document=self.spec.doc_id if self.spec is not None else None,
            backend=(
                self.template_name
                if self.template_seed_bytes is not None and delivered == self.template_seed_bytes
                else self.backend_name or None
            ),
            iterations=None if self.outcome is None else int(self.outcome.iterations),
            reinforcement=self.reinforcement,
        )
        self.card_path = next(
            (path for path in written if path.name == "MODEL_CARD.md"),
            self.out_dir / "MODEL_CARD.md",
        )
        if (
            self.reviewed_extraction is not None
            and self.reviewed_extraction.get("complete_datasheet_extraction") is False
        ):
            self._write_text(
                self.card_path,
                self.card_path.read_text(encoding="utf-8")
                + "\n## Reviewed evidence scope\n\n"
                + "This build uses a partial reviewed extraction, not every datasheet statement. "
                + str(self.reviewed_extraction.get("scope", ""))
                + "\nUnreviewed scope: "
                + str(self.reviewed_extraction.get("unreviewed_scope", "not qualified"))
                + "\nDetails and source identity are in `reviewed-extraction.json`.\n",
            )
        if self.template_seed is not None and self.template_seed_bytes is not None:
            same_as_seed = delivered == self.template_seed_bytes
            metadata = {
                **self.template_seed,
                "seed_sha256": hashlib.sha256(self.template_seed_bytes).hexdigest(),
                "final_model_sha256": hashlib.sha256(delivered).hexdigest(),
                "final_model_matches_seed": same_as_seed,
            }
            self._write_text(
                self.out_dir / "template-parameters.json",
                json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            )
            defaults = [
                parameter
                for parameter in metadata["parameters"]
                if parameter["origin"] == "template_default"
            ]
            provenance = [
                f"\n## {self.template_heading} parameter origins\n",
                "The starting template and its cited rows are recorded in "
                "`template-parameters.json`. This file is provenance, not verification.\n",
            ]
            if same_as_seed:
                provenance.append(
                    "The delivered library matches the simulator-judged template seed.\n"
                )
                if defaults:
                    provenance.append("Template defaults without a cited datasheet value:\n")
                    provenance.extend(
                        f"- `{entry['name']}` = {entry['value']:g} {entry['unit']} "
                        "(template default)\n"
                        for entry in defaults
                    )
            else:
                provenance.append(
                    "The agent changed the seed during repair. Parameter origins in the JSON "
                    "describe the starting seed; review the delivered library for final values.\n"
                )
            if metadata.get("limitations"):
                provenance.append("\n## Modeled scope and limitations\n\n")
                provenance.extend(f"- {item}\n" for item in metadata["limitations"])
            if metadata.get("numerical_assumptions"):
                provenance.append(
                    "\n## Numerical assumptions\n\n"
                    "These values regularize the first-order implementation. They are not "
                    "measured device characteristics or qualified datasheet values.\n\n"
                )
                provenance.extend(
                    f"- `{item['name']}` = {item['value']:g} {item['unit']}: {item['reason']}\n"
                    for item in metadata["numerical_assumptions"]
                )
            self._write_text(
                self.card_path,
                self.card_path.read_text(encoding="utf-8") + "".join(provenance),
            )
            notes.append(f"saved {self.template_name.replace(chr(95), chr(32))} parameter origins")
        if self.pin_only_info is not None and self.lib_path is not None:
            info = self.pin_only_info
            checks = info["gate_checks"]
            summary = ", ".join(f"{c['id']} {c['status']}" for c in checks)
            source = (
                "the supplied extraction result"
                if request.requirements_json is not None
                else "the extraction from the datasheet"
            )
            section = [
                "## Pin-only model (limited)",
                "",
                "This model was requested as pin-only. It has the package pins, the supply draw, "
                "input clamps, finite output stages and wiring alarms, and it models no function: "
                "outputs stay high impedance until an instance parameter commands them, and no "
                "datasheet row was judged, so every row above is UNKNOWN or not applicable. It is "
                "not a substitute for a behavioural model.",
                "",
                f"Pin table: taken from {source}; the independent source confirmation is "
                "recorded in `pinout-report.json`. "
                f"Rail: {info['rail']}. Ground: {info['ground']}.",
                "",
            ]
            section += [f"- {note}" for note in info["notes"]]
            section += [
                "",
                f"Viability gate ({info['gate_benches']} benches, {info['gate_seconds']} s): "
                f"{summary}. Details are in `pin-only-report.json`.",
                "",
            ]
            self._write_text(
                self.card_path,
                self.card_path.read_text(encoding="utf-8") + "\n" + "\n".join(section),
            )
        if request.verification == "sanity":
            from boardmodeler.authoring.sanity import write_card

            write_card(
                self.card_path,
                self.spec,
                self.report.model_sha256,
                self.unverified,
                load=self._receipt_load(),
            )
            self._write_text(
                self.out_dir / "sanity-report.json",
                (self.workdir / "sanity-report.json").read_text(encoding="utf-8"),
            )
            self._write_text(
                self.out_dir / "example.cir",
                f"* {request.subckt}: connection template, not a verified test circuit\n"
                "* Add power supplies, inputs, loads and an analysis before simulation.\n"
                f".include {lib_target.name}\n"
                f"XU1 {' '.join(ports)} {request.subckt}\n.end\n",
            )
            # Replace any old full-mode report: this artifact has no simulated outcomes.
            self._write_text(self.out_dir / HARNESS_REPORT_NAME, self.report.to_json())
        example = self.out_dir / "example.cir"
        if example.is_file():
            self._write_text(self.out_dir / EXAMPLE_NAME, example.read_text(encoding="utf-8"))
        if self.report.outcomes:
            self._write_text(self.out_dir / HARNESS_REPORT_NAME, self.report.to_json())
        notes.append(
            f"published {lib_target.name}, {symbol.name}, MODEL_CARD.md, {EXAMPLE_NAME}, "
            f"{HARNESS_REPORT_NAME} and {SPEC_DIRNAME}/"
        )
        notes.append(
            f"model sha256 {self.report.model_sha256[:12] or 'unknown'}, "
            f"{self.turns} harness turn(s)"
        )
        if (
            lib_target.read_bytes() != delivered
            or symbol.read_bytes() != symbol_bytes
            or hashlib.sha256(delivered).hexdigest() != self.report.model_sha256
            or self.report.spec_digest != self.spec.digest()
        ):
            raise ValueError("publication_evidence_changed: delivered bytes or frozen spec changed")
        self._check_pinout_publication(delivered, symbol_bytes, lib_target.name)
        self._write_text(
            self.card_path,
            self.card_path.read_text(encoding="utf-8")
            + "\n## Package pinout confirmation\n\n"
            + "The source-confirmed physical pin numbers, terminal names and symbol SpiceOrder "
            + "are recorded with exact library/symbol hashes in `pinout-report.json`. "
            + "This is a model-symbol mapping check, not a PCB footprint or electrical qualification.\n",
        )

    def _publish_design_record(
        self, delivered: bytes, *, symbol: Path, ports: Sequence[str]
    ) -> None:
        """Every publication refreshes provenance, including resumed legacy repairs.

        A resumed run may have no in-memory seed. A prior design then describes
        only the old candidate; changing its library cannot preserve an exact
        association. Missing or unreadable provenance is explicitly unavailable.
        """
        path = self.out_dir / DESIGN_RECORD_NAME
        digest = hashlib.sha256(delivered).hexdigest()
        if self.template_design is not None:
            record = self.template_design.record(delivered)
        else:
            try:
                prior_path = path
                if not prior_path.is_file() and self.previous_publication_dir is not None:
                    prior_path = self.previous_publication_dir / DESIGN_RECORD_NAME
                previous = json.loads(prior_path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                previous = None
            if (
                isinstance(previous, dict)
                and previous.get("record_kind") == "model_design_record"
                and isinstance(previous.get("design"), dict)
                and isinstance(previous.get("rendered_library_sha256"), str)
            ):
                exact = self._saved_design_is_exact(previous, delivered)
                record = {
                    **previous,
                    "delivered_library_sha256": digest,
                    "association": "exact" if exact else "invalid_after_change",
                    "association_note": (
                        "The saved design was revalidated against the delivered library and spec."
                        if exact
                        else "The saved design could not be revalidated against the delivered "
                        "library and spec. It is retained as prior provenance only; final "
                        "parameters were not reconstructed from SPICE text."
                    ),
                    "verdict": "UNJUDGED",
                }
            else:
                record = {
                    "schema_version": 1,
                    "record_kind": "model_design_record",
                    "design": None,
                    "design_sha256": None,
                    "rendered_library_sha256": None,
                    "delivered_library_sha256": digest,
                    "association": "unavailable",
                    "association_note": "No usable typed design is associated with this library.",
                    "verdict": "UNJUDGED",
                }
        record["delivered_symbol_sha256"] = hashlib.sha256(symbol.read_bytes()).hexdigest()
        record["delivered_pin_order"] = list(ports)
        self._write_json(path, record)
        # A resumed run can also retain parameter provenance from its old seed.
        # Keep those origins, but never leave their delivered-byte claim stale.
        parameters_path = self.out_dir / "template-parameters.json"
        prior_parameters = parameters_path
        if not prior_parameters.is_file() and self.previous_publication_dir is not None:
            prior_parameters = self.previous_publication_dir / "template-parameters.json"
        if self.template_seed is None and prior_parameters.is_file():
            try:
                parameters = json.loads(prior_parameters.read_text(encoding="utf-8"))
            except OSError, ValueError:
                parameters = {}
            if not isinstance(parameters, dict):
                parameters = {}
            parameters.update(
                final_model_sha256=digest,
                final_model_matches_seed=parameters.get("seed_sha256") == digest,
                provenance_note="Retained starting-seed parameters; final parameters not inferred.",
            )
            self._write_json(parameters_path, parameters)

    def _saved_design_is_exact(self, record: Any, delivered: bytes) -> bool:
        """Validate saved values against the fixed source spec, then exact rendered bytes."""
        from boardmodeler.authoring.retest import saved_design_is_exact

        return self.spec is not None and saved_design_is_exact(
            record, delivered, self.spec, unverified=self.unverified
        )

    def _publish_symbol(
        self, ports: Sequence[str], lib_name: str, *, destination: Path | None = None
    ) -> tuple[Path, str]:
        subckt = self.request.subckt
        target = destination or self.out_dir / f"{subckt}.asy"
        # Appearance is always application-owned, including cached builds. Pin-map
        # directions affect placement only; the .subckt defines electrical order.
        directions = {
            str(pin["name"]): str(pin["direction"])
            for pin in self.pin_map
            if pin.get("name") in ports and pin.get("direction")
        }
        note = "standard symbol regenerated locally from the model's declared ports"
        write_symbol_for(
            out_path=target,
            name=subckt,
            ports=ports,
            model_file=lib_name,
            model_name=subckt,
            description=f"{subckt} authored model",
            directions=directions,
        )
        return target, note

    def _write_text(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    # ----------------------------------------------------------------- result

    def rows(self) -> tuple[RowOutcome, ...]:
        if self.spec is None:
            return ()
        if self.request.verification == "sanity":
            return tuple(
                RowOutcome(
                    c.char_id,
                    c.statement,
                    _required_text(c),
                    "not simulated",
                    "UNKNOWN",
                    c.source_page,
                )
                for c in self.spec.characteristics
            )
        outcomes = {
            char_id: outcome for outcome in self.report.outcomes for char_id in outcome.char_ids
        }
        rows: list[RowOutcome] = []
        for characteristic in self.spec.characteristics:
            req_id = characteristic.char_id
            required = _required_text(characteristic)
            if req_id in self.unverified:
                rows.append(
                    RowOutcome(
                        req_id=req_id,
                        statement=characteristic.statement,
                        required=f"citation unverified: {self.unverified[req_id]}",
                        measured="-",
                        status=Status.UNKNOWN.value,
                        page=characteristic.source_page,
                    )
                )
                continue
            if characteristic.probe is None:
                missing_numeric_test = (
                    characteristic.req_class in ("DOCUMENTED_LIMIT", "TYPICAL_VALUE")
                    and any(
                        value is not None
                        for value in (
                            characteristic.min_value,
                            characteristic.max_value,
                            characteristic.typ_value,
                        )
                    )
                    and not re.search(
                        r"recommended|operating (?:range|envelope)", characteristic.statement, re.I
                    )
                )
                rows.append(
                    RowOutcome(
                        req_id=req_id,
                        statement=characteristic.statement,
                        required=characteristic.not_testable_reason or "no simulation probe",
                        measured="-",
                        status=Status.UNKNOWN.value
                        if missing_numeric_test
                        else Status.NOT_APPLICABLE.value,
                        page=characteristic.source_page,
                    )
                )
                continue
            outcome = outcomes.get(req_id)
            measured = "-"
            status = Status.UNKNOWN.value
            if outcome is not None:
                status, _detail = judge_characteristic(characteristic, outcome)
                measured = outcome.judged or "-"
                if status == Status.UNKNOWN.value and outcome.unknown_reason:
                    measured = f"- ({outcome.unknown_reason})"
            rows.append(
                RowOutcome(
                    req_id=req_id,
                    statement=characteristic.statement,
                    required=required,
                    measured=measured,
                    status=status,
                    page=characteristic.source_page,
                )
            )
        return tuple(rows)

    def decide(self, rows: Sequence[RowOutcome]) -> tuple[str, str]:
        """``(status, detail)`` for this run, with the next action in the detail."""
        outcome = self.outcome
        if self.status == Status.BLOCKED.value:
            return self.status, self.detail
        if self.publication_problem:
            return Status.UNKNOWN.value, self.detail
        if outcome is None:
            reason = self.detail or "the build did not reach the authoring stage"
            return self.status, reason
        if outcome.status == Status.BLOCKED.value:
            return Status.BLOCKED.value, outcome.detail
        if outcome.status == Status.PASS.value:
            incomplete = [row.req_id for row in rows if row.status == Status.UNKNOWN.value]
            if incomplete:
                return (
                    Status.UNKNOWN.value,
                    "every bound row passed its simulator run, but "
                    f"{len(incomplete)} row(s) remain unverified or lack a measurement "
                    f"({', '.join(incomplete[:5])}). The model is available with limited coverage; "
                    "review the UNKNOWN rows and model card before using it outside the tested conditions.",
                )
            judged = sum(1 for row in rows if row.status in (Status.PASS.value, Status.FAIL.value))
            return (
                Status.PASS.value,
                f"{judged} measured row(s) passed real LTspice runs at the recorded operating points; "
                f"{len(rows) - judged} other row(s) remain outside the tested scope with a "
                f"reason. The model is in {self.out_dir}; open "
                f"{self.request.subckt}.asy in LTspice or run 'boardmodeler model install "
                f"--out {self.out_dir} --user-lib --apply'.",
            )
        if _confirmed_wrong(outcome, self.request.max_iterations):
            failing = ", ".join(o.probe_id for o in outcome.report.failing())
            return (
                Status.FAIL.value,
                f"the harness measured the model outside the datasheet limits after "
                f"{outcome.iterations} turn(s) (failing: {failing}); review those rows in "
                f"{self.out_dir / 'MODEL_CARD.md'} and re-run with more turns or a fixed model",
            )
        return Status.UNKNOWN.value, outcome.detail


def _confirmed_wrong(outcome: BuildOutcome, max_iterations: int | None) -> bool:
    """The loop used its whole budget and the harness measured every bound row.

    "Harness-confirmed" is the point: a model that is judged wrong in every
    probed respect at the cap is FAIL, while a model the harness could not judge
    (or a run that stopped early) stays UNKNOWN — an unmeasured row is never
    evidence of wrongness. An uncapped build has no cap to exhaust, so it can
    never be FAIL this way; a stalled one keeps its UNKNOWN with the reason.
    """
    return (
        max_iterations is not None
        and outcome.status == Status.UNKNOWN.value
        and outcome.detail.startswith(_CAP_PREFIX)
        and outcome.iterations == max_iterations
        and bool(outcome.report.failing())
        and not outcome.report.unknown()
    )


def _apply_qualification_status(
    status: str, detail: str, report: dict[str, Any] | None
) -> tuple[str, str]:
    """A measured fixed-check failure cannot disappear behind the row-harness verdict."""
    if report is None:
        return status, detail
    verdict = report.get("status")
    counts = report.get("counts") or {}
    summary = (
        f"{counts.get('PASS', 0)} PASS, {counts.get('FAIL', 0)} FAIL, "
        f"{counts.get('UNKNOWN', 0)} UNKNOWN, {counts.get('BLOCKED', 0)} BLOCKED"
    )
    if verdict == Status.FAIL.value:
        return Status.FAIL.value, (
            f"independent fixed qualification measured a failure ({summary}); {detail}"
        )
    if verdict == Status.BLOCKED.value:
        return Status.BLOCKED.value, (
            f"independent fixed qualification is BLOCKED ({summary}); "
            f"see qualification-report.json for the required simulation refusal; {detail}"
        )
    if verdict == Status.UNKNOWN.value and status == Status.PASS.value:
        return Status.UNKNOWN.value, (
            f"row harness passed, but independent fixed qualification is {verdict} "
            f"({summary}); see qualification-report.json for missing mandatory checks"
        )
    return status, detail


# --------------------------------------------------------------------------- #
# entry point


#: Backend names whose author answers over the network; the offline authors are
#: ``scripted``/``fixture``, which never do. An empty name means the default, ``api``.
_NETWORK_BACKENDS = frozenset({"", "api", "bob"})


def _author_needs_network(request: MakeModelRequest) -> bool:
    """True when this request's author would send something to a provider.

    Read before the stages run so a switched-off network is reported immediately, and
    kept in one place so :func:`build_backend` and the pre-flight check cannot disagree.
    """
    if request.engine in ("behavioral", "pin_only"):
        # no agent author runs on these routes; extraction, when the rows are not supplied,
        # refuses on its own with the reason if it needs the network and the switch is off
        return False
    return str(request.backend_name or "").strip().lower() in _NETWORK_BACKENDS


@_exclusive_model_build
def make_model(
    request: MakeModelRequest,
    progress: Callable[[StageEvent], None] | None = None,
    cancel: threading.Event | None = None,
) -> MakeModelResult:
    """Make, judge and save an LTspice model for ``request.part``.

    Never raises for an expected failure; see the module docstring for the stage
    contract and the status ladder. ``progress`` receives every
    :class:`StageEvent` in order (the ``judge`` events carry the harness's own
    per-turn counts); ``cancel`` stops the agent loop and the harness at the next
    safe point and reports ``UNKNOWN(cancelled: ...)``.
    """
    request = _checked(request)
    log = _StageLog(progress)
    run = _Run(request, log)
    local_spec_supplied = (
        request.requirements_json is not None and request.bindings_json is not None
    )
    try:
        run.out_dir.mkdir(parents=True, exist_ok=True)
        run._archive_deliverables(include_diagnostics=True)
    except OSError as exc:
        detail = f"output_dir_unusable: {type(exc).__name__}: {exc}"
        log.emit("read", "failed", detail)
        return _empty_result(request, log, detail)
    ledger = _Ledger()
    try:
        if _author_needs_network(request) and not internet_allowed() and not local_spec_supplied:
            # Refuse before provider work, using the same withdrawal/result path
            # as later refusals so a reused folder cannot present an old model.
            raise _Stop(
                "author",
                Status.BLOCKED.value,
                refusal_detail("authoring a model over the agent API"),
            )
        with ledger.stage("read"):
            run.read()
        from boardmodeler.pipeline.official_delivery import try_official_delivery

        with ledger.stage("official"):
            try:
                official = try_official_delivery(
                    request, run.record, run.head_text, run.out_dir, log, cancel
                )
            except (OSError, ValueError) as exc:
                raise _Stop(
                    "official", Status.BLOCKED.value, f"official_delivery_failed: {exc}"
                ) from exc
        if official is not None:
            timing = ledger.payload(
                part=request.part,
                status=official.status,
                route="official_manufacturer_original",
                **run._provider_call_fields(),
            )
            run._write_text(
                run.out_dir / TIMING_NAME, json.dumps(timing, indent=2, sort_keys=True) + "\n"
            )
            return official
        with ledger.stage("preflight"):
            run.preflight()
        with ledger.stage("extract"):
            run.extract(cancel)
        with ledger.stage("bind"):
            run.bind(cancel)
        with ledger.stage("gate"):
            run.support_gate()
        with ledger.stage("pinout"):
            run.freeze_pinout()
        with ledger.stage("qualification_plan"):
            run.freeze_qualification()
        with ledger.stage("author"):
            run.author(cancel)
        with ledger.stage("save"):
            run.save()
        with ledger.stage("qualification"):
            run.qualify(cancel)
    except _Stop as stop:
        log.emit(stop.stage, "failed", stop.detail)
        run.status, run.detail = stop.status, stop.detail
        with ledger.stage("save"):
            run.save()
    rows = run.rows()
    status, detail = run.decide(rows)
    status, detail = _apply_qualification_status(status, detail, run.qualification_report)
    result = MakeModelResult(
        status=status,
        detail=detail,
        part=request.part,
        out_dir=run.out_dir,
        card_path=run.card_path,
        lib_path=run.lib_path,
        asy_path=run.asy_path,
        rows=rows,
        counts=status_tally(row.status for row in rows),
        stages=tuple(log.events),
        request=request,
    )
    try:
        run._write_text(run.out_dir / RESULTS_NAME, result.to_json())
    except OSError as exc:
        note = f"{RESULTS_NAME} could not be written: {type(exc).__name__}: {exc}"
        log.emit("save", "failed", note, {"files": 0})
        result = dataclasses.replace(
            result, detail=f"{result.detail}; {note}", stages=tuple(log.events)
        )
    if "author" not in ledger.seconds:
        timing_route = "refused_before_authoring"
    elif request.engine == "pin_only":
        timing_route = "pin_only_shell" if run.pin_only_info is not None else "pin_only_refused"
    elif request.engine == "behavioral":
        timing_route = (
            f"{run.template_name}_seed" if run.template_seed is not None else "behavioral_refused"
        )
    else:
        timing_route = "agent_authoring"
    with contextlib.suppress(OSError):  # a courtesy record: it never changes the verdict
        timing = ledger.payload(
            part=request.part,
            status=status,
            route=timing_route,
            template_seed_judge_seconds=(
                None if run.template_seed_judge_s is None else round(run.template_seed_judge_s, 3)
            ),
            template_compile_seconds=(
                None if run.template_compile_s is None else round(run.template_compile_s, 3)
            ),
            simulation_seconds=(
                None
                if run.template_seed_judge_s is None and run.qualification_report is None
                else round(
                    (run.template_seed_judge_s or 0.0)
                    + (
                        0.0
                        if run.qualification_report is None
                        else float(run.qualification_report.get("simulation_seconds", 0.0))
                    ),
                    3,
                )
            ),
            qualification_simulation_seconds=(
                None
                if run.qualification_report is None
                else round(float(run.qualification_report.get("simulation_seconds", 0.0)), 3)
            ),
            qualification_status=(
                None if run.qualification_report is None else run.qualification_report.get("status")
            ),
            author_turns=None if run.outcome is None else int(run.outcome.iterations),
            **run._provider_call_fields(),
        )
        run._write_text(
            run.out_dir / TIMING_NAME,
            json.dumps(timing, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
    return result


def _empty_result(request: MakeModelRequest, log: _StageLog, detail: str) -> MakeModelResult:
    return MakeModelResult(
        status=Status.BLOCKED.value,
        detail=detail,
        part=request.part,
        out_dir=Path(request.out_dir),
        card_path=None,
        lib_path=None,
        asy_path=None,
        rows=(),
        counts={status.value: 0 for status in Status},
        stages=tuple(log.events),
    )


# --------------------------------------------------------------------------- #
# reopening a model that is already on disk


MODEL_CARD_NAME = "MODEL_CARD.md"
"""The card every published model directory carries (see ``authoring.card``)."""

_NOT_A_MODEL = (
    "not_a_model_directory: {directory} holds none of {results}, {card}, "
    "spec/{characteristics} or {report}; choose the folder a build wrote"
)


@dataclass(frozen=True)
class ModelSummary:
    """What an existing model directory honestly is, read from disk and never guessed.

    ``reason`` is what makes this honest: a directory that is not a model comes back with
    ``reason="not_a_model_directory"`` and no invented part, so a caller refuses (or says
    why) instead of showing an empty model. ``result`` is the recorded run — status, row
    counts and every row — only when ``results.json`` parsed; ``results_problem`` says what
    stopped it otherwise, because "I could not read the record" is a fact a user needs.

    ``manifest_at`` is ``build/project.json``'s own ``created_utc`` when it carries one
    (else that file's modification time), and ``verification_at`` is when
    ``harness-report.json`` was last written: the verification evidence's timestamp, not
    the build's. Both are ISO-8601 UTC strings, and ``None`` when the file is absent.
    """

    out_dir: Path
    reason: str | None
    result: MakeModelResult | None = None
    part: str | None = None
    subckt: str | None = None
    datasheet: Path | None = None
    card_path: Path | None = None
    lib_path: Path | None = None
    asy_path: Path | None = None
    lib_exists: bool = False
    asy_exists: bool = False
    results_path: Path | None = None
    results_problem: str | None = None
    manifest_path: Path | None = None
    manifest_at: str | None = None
    verification_path: Path | None = None
    verification_at: str | None = None

    @property
    def ok(self) -> bool:
        """True when the directory holds a model, so the summary may be shown."""
        return self.reason is None

    @property
    def status(self) -> str | None:
        """The recorded verdict, or ``None`` when ``results.json`` did not yield one."""
        return None if self.result is None else self.result.status

    @property
    def detail(self) -> str:
        """The recorded reason for that verdict, or the reading problem instead."""
        if self.result is not None:
            return self.result.detail
        return self.results_problem or self.reason or ""

    @property
    def counts(self) -> dict[str, int]:
        """The recorded row counts, empty when nothing was readable."""
        return {} if self.result is None else dict(self.result.counts)

    @property
    def rows(self) -> tuple[RowOutcome, ...]:
        """The recorded rows; empty when nothing was readable, never re-derived."""
        return () if self.result is None else tuple(self.result.rows)


def spec_json_in(out_dir: str | Path) -> Path | None:
    """The frozen specification under ``out_dir``, or ``None`` when there is none."""
    for parts in ((SPEC_DIRNAME,), (WORK_DIRNAME, SPEC_DIRNAME)):
        candidate = Path(out_dir).joinpath(*parts, CHARACTERISTICS_NAME)
        if candidate.is_file():
            return candidate
    return None


def model_lib_in(out_dir: str | Path, subckt: str | None) -> Path | None:
    """The model's ``<SUBCKT>.lib``, looking where a build may have published it."""
    from boardmodeler.authoring.loop import MODEL_DIRNAME

    name = str(subckt or "").strip()
    if not name:
        return None
    for parts in ((), (MODEL_DIRNAME,), (WORK_DIRNAME, MODEL_DIRNAME)):
        candidate = Path(out_dir).joinpath(*parts, f"{name}.lib")
        if candidate.is_file():
            return candidate
    return None


def _modified_at(path: Path) -> str | None:
    """``path``'s modification time as an ISO-8601 UTC string, or ``None`` if unreadable."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat(timespec="seconds")


def _manifest_at(path: Path) -> str | None:
    """The manifest's own timestamp, falling back to when the file was last written."""
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return _modified_at(path)
    if isinstance(recorded, dict):
        claimed = recorded.get("created_utc")
        if isinstance(claimed, str) and claimed.strip():
            return claimed.strip()
    return _modified_at(path)


def _spec_parts(path: Path) -> tuple[str | None, str | None, str | None]:
    """``(part, subckt, problem)`` from a frozen specification file, never raising.

    A helper rather than an inline block so the reading of a file that may be truncated or
    hand-edited has exactly one boundary, and so the caller's own logic stays readable.
    """
    try:
        spec = SpecSet.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, None, f"spec could not be read: {type(exc).__name__}: {exc}"
    return spec.part or None, spec.subckt or None, None


def _summary_local_path(directory: Path | None, value: str | Path) -> Path:
    """Refuse network names and links before resolving saved artifact paths."""
    raw = str(value)
    if raw.replace("\\", "/").startswith("//") or "://" in raw:
        raise ValueError("saved model path is a network/device location")
    candidate = Path(raw)
    if directory is not None:
        if ".." in candidate.parts:
            raise ValueError("saved artifact path contains parent traversal")
        candidate = candidate if candidate.is_absolute() else directory / candidate
        if not candidate.is_relative_to(directory):
            raise ValueError("saved artifact path is outside the selected model folder")
        parts = candidate.relative_to(directory).parts
        if any(":" in part for part in parts):
            raise ValueError("saved artifact path names an alternate stream")
        cursor = directory
    else:
        candidate = candidate.absolute()
        cursor = Path(candidate.anchor)
        parts = candidate.parts[1:]
    for part in parts:
        cursor /= part
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("saved model path passes through a link or junction")
    resolved = candidate.resolve()
    if directory is not None and not resolved.is_relative_to(directory):
        raise ValueError("saved artifact resolves outside the selected model folder")
    return resolved


def _summary_official_artifacts(directory: Path) -> tuple[Path, Path]:
    """Reopen exact imported files, including dependencies, without inventing accuracy."""
    receipt_path = _summary_local_path(directory, "official-model.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "delivered"
        or receipt.get("adaptation_applied") is not False
        or receipt.get("electrical_accuracy_verified") is not False
    ):
        raise ValueError("official saved receipt does not describe an unchanged delivered original")
    root = _summary_local_path(directory, receipt["bundle_root"])
    model = _summary_local_path(directory, receipt["model_path"])
    if not model.is_relative_to(root):
        raise ValueError("official model is outside its recorded bundle")
    files = receipt.get("files")
    if not isinstance(files, dict) or not 0 < len(files) <= 200:
        raise ValueError("official dependency manifest is missing or excessive")
    total = 0
    for relative, expected in files.items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError("official dependency manifest has an invalid path or digest")
        artifact = _summary_local_path(root, relative)
        size = artifact.stat().st_size
        total += size
        if size > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
            raise ValueError("official saved bundle exceeds acquisition limits")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise ValueError("official original or dependency bytes changed since delivery")
    relative_model = model.relative_to(root).as_posix()
    if files.get(relative_model) != receipt.get("model_sha256"):
        raise ValueError("official model identity is not bound to the dependency manifest")
    package_path = receipt.get("source_package_path")
    if package_path is not None:
        package = _summary_local_path(directory, package_path)
        if package.stat().st_size > 32 * 1024 * 1024 or (
            hashlib.sha256(package.read_bytes()).hexdigest() != receipt.get("source_sha256")
        ):
            raise ValueError("official original source package bytes changed since delivery")
    symbol = _summary_local_path(directory, receipt["symbol_path"])
    if symbol.stat().st_size > 1024 * 1024 or (
        hashlib.sha256(symbol.read_bytes()).hexdigest() != receipt.get("symbol_sha256")
    ):
        raise ValueError("official saved symbol bytes changed since delivery")
    return model, symbol


def load_model_summary(out_dir: str | Path) -> ModelSummary:
    """Read an existing model directory: what it holds, and what it recorded.

    Never guesses and never raises for a directory that is not a model — the caller gets
    ``reason="not_a_model_directory"`` instead of an invented part number, and an I/O or
    parse problem is reported in ``results_problem`` rather than hidden. A directory is
    accepted as a model when it carries any of ``results.json``, ``MODEL_CARD.md``,
    ``spec/characteristics.json`` or ``harness-report.json``: those are the files a build
    writes, and requiring all of them would refuse the half-finished directory a user is
    most likely to ask about.
    """
    try:
        directory = _summary_local_path(None, out_dir)
    except (OSError, ValueError) as exc:
        return ModelSummary(out_dir=Path(out_dir), reason=f"model_directory_refused: {exc}")
    if not directory.is_dir():
        return ModelSummary(
            out_dir=directory, reason=f"not_a_directory: {directory} does not exist"
        )
    try:
        results_path = _summary_local_path(directory, RESULTS_NAME)
        spec_path = None
        for relative in (f"spec/{CHARACTERISTICS_NAME}", f"build/spec/{CHARACTERISTICS_NAME}"):
            candidate = _summary_local_path(directory, relative)
            if candidate.is_file():
                spec_path = candidate
                break
        card = _summary_local_path(directory, MODEL_CARD_NAME)
        report = _summary_local_path(directory, HARNESS_REPORT_NAME)
    except (OSError, ValueError) as exc:
        return ModelSummary(out_dir=directory, reason=f"model_directory_refused: {exc}")
    markers = [path for path in (results_path, spec_path, card, report) if path is not None]
    if not any(path.is_file() for path in markers):
        return ModelSummary(
            out_dir=directory,
            reason=_NOT_A_MODEL.format(
                directory=directory,
                results=RESULTS_NAME,
                card=MODEL_CARD_NAME,
                characteristics=CHARACTERISTICS_NAME,
                report=HARNESS_REPORT_NAME,
            ),
        )

    result: MakeModelResult | None = None
    problem: str | None = None
    if results_path.is_file():
        try:
            result = MakeModelResult.from_json(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problem = f"{RESULTS_NAME} could not be read: {type(exc).__name__}: {exc}"

    part: str | None = None if result is None else (result.part or None)
    subckt: str | None = (
        None if result is None or result.request is None else (result.request.subckt or None)
    )
    datasheet: Path | None = (
        None if result is None or result.request is None else (result.request.datasheet)
    )
    if subckt is None and spec_path is not None:
        spec_part, spec_subckt, spec_problem = _spec_parts(spec_path)
        part = part or spec_part
        subckt = subckt or spec_subckt
        problem = problem or spec_problem

    def saved_file(value):
        if value is None:
            return None
        try:
            path = _summary_local_path(directory, value)
            return path if path.is_file() else None
        except OSError, ValueError:
            return None

    published_lib = None if result is None else result.lib_path
    published_asy = None if result is None else result.asy_path
    lib_path = saved_file(published_lib)
    asy_path = saved_file(published_asy)
    official = directory / "official-model.json"
    official_problem = None
    if official.is_symlink() or official.is_file():
        try:
            receipt = json.loads(
                _summary_local_path(directory, official).read_text(encoding="utf-8")
            )
            if isinstance(receipt, dict) and receipt.get("status") == "delivered":
                lib_path, asy_path = _summary_official_artifacts(directory)
                if result is not None:
                    result = dataclasses.replace(
                        result,
                        status="UNKNOWN",
                        counts={"PASS": 0, "FAIL": 0, "UNKNOWN": 1, "NOT_APPLICABLE": 0},
                    )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            official_problem = f"official saved artifacts could not be verified: {exc}"
            problem = official_problem
            lib_path = asy_path = None
            if result is not None:
                result = dataclasses.replace(result, status="BLOCKED", detail=official_problem)
    simple_subckt = bool(subckt) and not any(char in subckt for char in "/\\:")
    lib_path = lib_path or (
        None
        if official_problem or not simple_subckt
        else saved_file(model_lib_in(directory, subckt))
    )
    if asy_path is None and subckt:
        candidate = directory / f"{subckt}.asy"
        if not official_problem:
            asy_path = saved_file(candidate)
    card_path = None if not card.is_file() else card
    if card_path is None and result is not None and result.card_path is not None:
        card_path = saved_file(result.card_path)
    if result is not None:
        result = dataclasses.replace(
            result,
            out_dir=directory,
            card_path=card_path,
            lib_path=lib_path,
            asy_path=asy_path,
        )

    manifest_path = directory / WORK_DIRNAME / "project.json"
    return ModelSummary(
        out_dir=directory,
        reason=None,
        result=result,
        part=part,
        subckt=subckt,
        datasheet=datasheet,
        card_path=card_path,
        lib_path=lib_path,
        asy_path=asy_path,
        lib_exists=lib_path is not None,
        asy_exists=asy_path is not None,
        results_path=results_path if results_path.is_file() else None,
        results_problem=problem,
        manifest_path=manifest_path if manifest_path.is_file() else None,
        manifest_at=_manifest_at(manifest_path) if manifest_path.is_file() else None,
        verification_path=report if report.is_file() else None,
        verification_at=_modified_at(report) if report.is_file() else None,
    )
