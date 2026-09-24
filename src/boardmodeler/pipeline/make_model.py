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
``api`` path (including a Bob API key) applies a finite default of
:data:`~boardmodeler.authoring.api_backend.DEFAULT_TIMEOUT_S` (600 s) to each
turn when the caller leaves ``turn_timeout_s`` unset; an explicit value overrides
it, and the loop API and direct Bob CLI path stay unbounded when called with
``None``. ``timeout_s`` bounds a single simulation run, not the build.

Nothing raises for an expected failure — a missing datasheet, a document the
provider refuses, a missing key, absent LTspice, a tampered spec or a
cancellation all come back as a status with the observed reason. Only
programming errors (an unsanitised subcircuit name, a non-positive iteration cap)
raise.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
from boardmodeler.config import load_config
from boardmodeler.documents.pdf import page_text, read_pdf
from boardmodeler.documents.store import DocumentStore, DocumentStoreError
from boardmodeler.domain.enums import RequirementClass, RequirementOrigin, Status
from boardmodeler.domain.records import DocumentRecord, Requirement
from boardmodeler.models.library import ModelStoreError, subckt_ports
from boardmodeler.models.symbolism import symbol_text
from boardmodeler.providers.agent import AgentExtractionProvider
from boardmodeler.providers.base import ProviderError
from boardmodeler.providers.registry import select_provider
from boardmodeler.requirements.model import validate_requirements
from boardmodeler.requirements.review import apply_review, verify_citations
from boardmodeler.simulation.ltspice import locate

__all__ = [
    "MakeModelRequest",
    "MakeModelResult",
    "RowOutcome",
    "StageEvent",
    "bind_requirements",
    "build_backend",
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
EXAMPLE_NAME = "EXAMPLE.cir"

LTSPICE_MISSING = (
    "ltspice_not_configured: open SETUP, choose LTspice.exe, save, then re-run "
    "'boardmodeler doctor' to confirm it"
)

_TEXT_SUFFIXES = frozenset({".txt", ".text", ".md"})

#: The loop reports an exhausted iteration budget with this prefix; it is the
#: only terminal reason that means "the agent ran out of turns with the harness
#: still measuring the model wrong" rather than "the build stopped early".
_CAP_PREFIX = "max_iterations="

#: Serialises the harness instrumentation (see :func:`_observe_reports`). One
#: process runs one model build at a time; a second concurrent call is reported
#: rather than interleaving two builds' harness reports.
_BUILD_LOCK = threading.Lock()

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
    return _JUDGE_UNITS.get(probe.judge_key, "")


# --------------------------------------------------------------------------- #
# records


@dataclass(frozen=True)
class MakeModelRequest:
    """What the caller asks for: a part, its datasheet, and where to save it.

    ``max_iterations=None`` (the default) has no cap: the author loop runs until
    the harness is satisfied or the agent stops making progress, which is what
    ``stall_patience`` counts. ``turn_timeout_s`` bounds one agent invocation; when
    it is ``None`` the product ``api`` path (including a Bob API key) applies its own
    finite 600 s default, and an explicit value overrides that. ``timeout_s`` bounds
    a single simulation run. There is no build deadline.

    ``backend_name`` names the author: ``"api"`` (the default) uses an API-key
    provider — ``provider`` is a provider id from
    :mod:`boardmodeler.agent_providers`, ``agent_model`` overrides its
    documented model and ``agent_max_tokens`` its output budget, all falling back
    to the persisted settings and then to the catalog's own defaults — ``"bob"``
    runs the Bob CLI, and ``"scripted"``/``"fixture"`` write the bundled offline
    template.
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
    settings. ``bob`` is the Bob CLI. ``scripted``/``fixture`` name the offline
    author: it writes the bundled behavioural regulator template (and a symbol for
    it) when ``request.subckt`` names one, and writes nothing otherwise — the
    harness then reports the missing model with its own reason. It is what the
    GUI's integration runs and anyone without an agent key use; it never pretends
    to have authored a model it did not write. An unknown name is refused by name
    (never substituted). The offline tests override this function to inject their
    own scripted backend.
    """
    name = str(request.backend_name or "").strip().lower()
    if name in ("", "api"):
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
        return BobShellBackend(team_id=request.team_id, timeout_s=request.turn_timeout_s)
    if name in ("scripted", "fixture"):
        return _bundled_author(request)
    return UnavailableBackend(
        name or "unknown",
        f"{name or 'unknown'}_backend_unavailable: unknown backend name; use 'api', 'bob', "
        "'scripted' or 'fixture'",
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


@contextlib.contextmanager
def _observe_reports(on_report: Callable[[HarnessReport], None]):
    """Route every harness report the author loop produces to ``on_report``.

    ``build_model`` offers no per-turn hook, so the one seam it exposes — the
    module-level ``run_harness`` name — is wrapped for the duration of the build
    and restored in a ``finally``. :data:`_BUILD_LOCK` keeps two concurrent
    builds in one process from swapping each other's wrapper.
    """
    from boardmodeler.authoring import loop as loop_module

    original = loop_module.run_harness

    def wrapper(
        *,
        model_lib: Path,
        subckt: str,
        spec: SpecSet,
        workdir: Path,
        ltspice: Path,
        timeout_s: float = 120.0,
        cancel: threading.Event | None = None,
    ) -> HarnessReport:
        report = original(
            model_lib=model_lib,
            subckt=subckt,
            spec=spec,
            workdir=workdir,
            ltspice=ltspice,
            timeout_s=timeout_s,
            cancel=cancel,
        )
        on_report(report)
        return report

    loop_module.run_harness = wrapper
    try:
        yield
    finally:
        loop_module.run_harness = original


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

    def lookup(doc_id: str, pdf_page: int) -> str | None:
        nonlocal document
        if doc_id != record.doc_id:
            return None
        try:
            path = store.original_path(doc_id)
        except KeyError, DocumentStoreError, OSError, ValueError:
            return None
        if path.suffix.lower() in _TEXT_SUFFIXES:
            if pdf_page != 0:
                return None
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        if document is None:
            try:
                document = read_pdf(path)
            except Exception:
                return None
        if pdf_page >= len(document.pages):
            return None
        return page_text(document, pdf_page)

    return lookup


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
        self.backend_name = ""
        self.turns = 0
        self.sanity_ok = False
        self.reference_bindings: list[dict[str, Any]] | None = None
        self.citation_lookup = None
        self.status = Status.UNKNOWN.value
        self.detail = ""
        self.lib_path: Path | None = None
        self.asy_path: Path | None = None
        self.card_path: Path | None = None
        self.files: list[Path] = []

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
                self.pin_map = tuple(
                    PinDefinition.model_validate(pin).model_dump(mode="json")
                    for pin in saved.get("pin_map", [])
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
        # The refusal sits here because this is the first point where both the part
        # number and the document's own text are in hand, and it is long before the
        # extraction, the agent and the simulator: a part the probes cannot judge is
        # stopped without a turn being spent. The stage's own writes (the registered
        # document) have already happened; the refusal adds none, and save() publishes
        # nothing for a run that never reached the authoring stage.
        refusal = classify(request.part, text=self.record.title)
        if not refusal.supported:
            raise _Stop("read", Status.BLOCKED.value, refusal.detail)
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
            from boardmodeler.authoring.lm358_reference import matches, records

            if matches(self.request.part, self.record.file_hash):
                self.citation_lookup = _page_lookup(self.record, self.store)
                page = self.citation_lookup(self.record.doc_id, 9)
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
                self.backend = build_backend(self.request)
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
        """Fill :attr:`unverified` (req_id -> reason) for this run's rows.

        With the cited document registered and readable, the excerpts are checked
        against its own page text. Without it, a supplied extraction result cannot
        verify its own citations; the document must be available.
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
                req_id: (
                    "the excerpt is not on the page it cites, or the citation is incomplete "
                    f"(doc {self.record.doc_id})"
                    if not verified
                    else ""
                )
                for req_id, verified in checks.items()
                if not verified
            }
            return
        self.unverified = {
            requirement.req_id: (
                "the datasheet text for the cited document is not available, so the citation "
                "could not be verified"
            )
            for requirement in self.requirements
            if requirement.origin is RequirementOrigin.DOCUMENT
        }

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
        elif self.pin_map and self.request.backend_name not in ("fixture", "scripted"):
            from boardmodeler.authoring.test_planner import plan_bindings

            try:
                entries = plan_bindings(
                    self.requirements,
                    self.pin_map,
                    self.backend or build_backend(self.request),
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

    def author(self, cancel: threading.Event | None) -> None:
        self.log.emit("author", "running", f"checking the {self.request.backend_name!r} backend")
        backend = self.backend or build_backend(self.request)
        self.backend = backend
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
        from boardmodeler.authoring.validation_cache import read_report, validation_key

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
        key = validation_key(path, self.spec, install.path, self.request.timeout_s)
        cached = read_report(self.workdir / "validation-cache", key, self.spec, path)
        prechecked = cached is None and not (cancel and cancel.is_set())
        if prechecked:
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
            usable, reason = backend.availability()
            if not usable:
                self.log.emit("author", "failed", reason)
                self.status, self.detail = Status.BLOCKED.value, reason
                return
            self._gather_supporting_material(cancel)
            request = dataclasses.replace(
                request,
                supporting_context="\n".join(
                    f"{source.url} (SHA256 {source.sha256}): {source.excerpt}"
                    for source in (self.reinforcement.sources if self.reinforcement else ())
                    if source.retrieved and source.sha256 and source.excerpt
                ),
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
            with _observe_reports(self._on_report):
                outcome = build_model(request, cancel, candidate_revalidated=prechecked)
        finally:
            _BUILD_LOCK.release()
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
        enabled = self.request.reinforce
        if enabled is None:
            try:
                from boardmodeler.config import load_config

                enabled = bool(load_config().web_reinforcement)
            except Exception:  # pragma: no cover - a broken config must not stop a build
                enabled = True
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

    def save(self) -> None:
        self.log.emit("save", "running", f"publishing deliverables into {self.out_dir}")
        notes: list[str] = []
        source = None if self.spec is None else model_file(self.workdir, self.request.subckt)
        if self.request.verification == "sanity" and not self.sanity_ok:
            source = None
        if source is not None and source.is_file():
            try:
                self._publish(source, notes)
            except (OSError, ValueError, ModelStoreError) as exc:
                notes.append(f"the model could not be published: {type(exc).__name__}: {exc}")
                refusal = f"model_not_published: {exc}"
                self.detail = f"{self.detail}; {refusal}".strip("; ") if self.detail else refusal
                self.lib_path = None
                self.asy_path = None
                self.card_path = None
        else:
            notes.append(
                f"no model file was written, so only {SPEC_DIRNAME}/ and {RESULTS_NAME} describe "
                "this run"
            )
        published = [path for path in (self.lib_path, self.asy_path, self.card_path) if path]
        self.log.emit(
            "save",
            "ok" if self.lib_path is not None else "skipped",
            "; ".join(notes) or f"published {len(published)} model file(s)",
            {"files": len(published)},
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
        text = source.read_text(encoding="utf-8", errors="replace")
        self._refuse_known_invalid(text)
        ports = list(subckt_ports(text, request.subckt))
        if not ports:
            raise ValueError(f"{source} declares no .subckt {request.subckt}")
        lib_target = self._write_text(self.out_dir / f"{request.subckt}.lib", text)
        self.lib_path = lib_target
        symbol, note = self._publish_symbol(ports, lib_target.name)
        self.asy_path = symbol
        notes.append(note)
        written = write_deliverables(
            out_dir=self.out_dir,
            part=request.part,
            subckt=request.subckt,
            spec=self.spec,
            report=self.report,
            document=self.spec.doc_id if self.spec is not None else None,
            backend=self.backend_name or None,
            iterations=None if self.outcome is None else int(self.outcome.iterations),
            reinforcement=self.reinforcement,
        )
        self.card_path = next(
            (path for path in written if path.name == "MODEL_CARD.md"),
            self.out_dir / "MODEL_CARD.md",
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

    def _publish_symbol(self, ports: Sequence[str], lib_name: str) -> tuple[Path, str]:
        subckt = self.request.subckt
        target = self.out_dir / f"{subckt}.asy"
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


# --------------------------------------------------------------------------- #
# entry point


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
    try:
        run.out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        detail = f"output_dir_unusable: {type(exc).__name__}: {exc}"
        log.emit("read", "failed", detail)
        return _empty_result(request, log, detail)
    try:
        run.read()
        run.extract(cancel)
        run.bind(cancel)
        run.author(cancel)
        run.save()
    except _Stop as stop:
        log.emit(stop.stage, "failed", stop.detail)
        run.status, run.detail = stop.status, stop.detail
        run.save()
    rows = run.rows()
    status, detail = run.decide(rows)
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
