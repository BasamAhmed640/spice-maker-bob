"""Pipeline controller: the stage chain (D10, ``docs/INTERFACES.md`` §1).

``IDENTIFY → COLLECT_EVIDENCE → BUILD_REQUIREMENTS → REVIEW_SOURCES →
FREEZE_BASELINE → SELECT_MODEL → COMPILE_SIMULATE → EVALUATE → REPAIR → EXPORT``

Every stage does real work against the project directory and reports a
:class:`StageProgress`; statuses are data, so a run that cannot proceed says
``BLOCKED`` with the observed reason instead of inventing a verdict.

Rules this module owns (each is covered by ``tests/pipeline/``):

* **The baseline is frozen before any repair.** ``FREEZE_BASELINE`` runs before
  ``REPAIR`` in :data:`STAGE_ORDER` and writes ``baseline.json`` (requirements +
  tests, hashed). A repair step may only write under ``models/candidates/<n>/``;
  :func:`apply_repair` raises :class:`RepairViolation` for a tolerance
  relaxation, a test deletion, an evidence edit, a coverage narrowing, or a
  circuit edit, and the loop stops with ``UNKNOWN``.
* **A requirement or scope change is visible.** When the frozen content differs
  from the existing baseline, ``baseline_version`` is bumped and ``review.json``
  gains a :class:`ReviewItem` describing the change.
* **Repair is bounded.** The loop runs at most ``max_repair_iterations`` times
  and stops early when a repair does not improve the evaluated results.
* **Simulator unavailability is BLOCKED, never PASS.** The runner is still given
  the case so the deck and its hash are preserved as evidence.
* **Only the export computes receipts.** ``EXPORT`` calls
  :func:`boardmodeler.reporting.export.export_project`; nothing here writes an
  approval or qualification receipt.

Cancellation is honoured before every stage and passed into the runner and the
provider. ``deadline_s`` bounds the whole run: once exhausted, the remaining
stages report ``BLOCKED('deadline_exceeded')``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from boardmodeler.config import AppConfig, load_config
from boardmodeler.documents.store import DocumentStore, DocumentStoreError
from boardmodeler.domain.enums import Status
from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes
from boardmodeler.domain.records import (
    Finding,
    ModelCapability,
    PartIdentity,
    Requirement,
    ReviewItem,
    TestCase,
    TestResult,
)
from boardmodeler.models.library import ModelStore
from boardmodeler.pipeline.project import (
    BASELINE_FILENAME,
    PROJECT_FILENAME,
    Baseline,
    Project,
    ProjectError,
)
from boardmodeler.pipeline.runner import RunArtifacts, RunContext, run_deck_tests
from boardmodeler.providers.base import ProviderError
from boardmodeler.providers.registry import select_provider
from boardmodeler.reporting.export import export_project
from boardmodeler.requirements.extract import ExtractionResult, extract_requirements
from boardmodeler.requirements.review import apply_review
from boardmodeler.security.paths import PathGuardError, resolve_within
from boardmodeler.simulation.ltspice import default_lib_dir, locate
from boardmodeler.verification.engine import evaluate_case, gate_from_capability

__all__ = [
    "STAGE_ORDER",
    "PipelineController",
    "PipelineRequest",
    "PipelineResult",
    "RepairEdit",
    "RepairProposal",
    "RepairViolation",
    "Stage",
    "StageProgress",
    "apply_repair",
    "check_repair",
]


class Stage(StrEnum):
    """The pipeline stages, in execution order."""

    IDENTIFY = "IDENTIFY"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    BUILD_REQUIREMENTS = "BUILD_REQUIREMENTS"
    REVIEW_SOURCES = "REVIEW_SOURCES"
    FREEZE_BASELINE = "FREEZE_BASELINE"
    SELECT_MODEL = "SELECT_MODEL"
    COMPILE_SIMULATE = "COMPILE_SIMULATE"
    EVALUATE = "EVALUATE"
    REPAIR = "REPAIR"
    EXPORT = "EXPORT"


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.IDENTIFY,
    Stage.COLLECT_EVIDENCE,
    Stage.BUILD_REQUIREMENTS,
    Stage.REVIEW_SOURCES,
    Stage.FREEZE_BASELINE,
    Stage.SELECT_MODEL,
    Stage.COMPILE_SIMULATE,
    Stage.EVALUATE,
    Stage.REPAIR,
    Stage.EXPORT,
)

_FATAL_STAGES = frozenset({Stage.IDENTIFY, Stage.COLLECT_EVIDENCE})
_SEVERITY: dict[Status, int] = {
    Status.NOT_APPLICABLE: 0,
    Status.PASS: 1,
    Status.UNKNOWN: 2,
    Status.BLOCKED: 3,
    Status.FAIL: 4,
}
_CANDIDATES_DIR = ("models", "candidates")
_REPAIR_KINDS: tuple[str, ...] = (
    "model_text",
    "tolerance",
    "test_deletion",
    "evidence",
    "coverage",
    "circuit",
)
_FORBIDDEN_REPAIR_KINDS: dict[str, str] = {
    "tolerance": "a repair must not relax a frozen tolerance",
    "test_deletion": "a repair must not delete or weaken a frozen test",
    "evidence": "a repair must not alter source evidence",
    "coverage": "a repair must not narrow coverage",
    "circuit": "a repair must not edit the circuit",
}
_RESULTS_FILE = "results.json"
_REVIEW_FILE = "review.json"
_EXTRACTION_FILE = "evidence/extraction.json"


class RepairViolation(RuntimeError):
    """Raised when a repair step would weaken the frozen baseline."""


@dataclass(frozen=True)
class RepairEdit:
    """One edit a repair step wants to make.

    ``file`` is a path relative to the project root, ``path`` is a locator inside
    it (e.g. ``BM_REG_BUCK.switch_ron``), and ``old``/``new`` are the exact
    strings to replace (``old == ""`` creates/overwrites the file). ``kind``
    declares what the edit is; the five kinds that would weaken the baseline are
    refused outright.
    """

    file: str
    path: str
    old: str
    new: str
    kind: Literal["model_text", "tolerance", "test_deletion", "evidence", "coverage", "circuit"] = (
        "model_text"
    )


@dataclass(frozen=True)
class RepairProposal:
    """What one bounded repair iteration proposes to change."""

    description: str
    edits: tuple[RepairEdit, ...]

    def files(self) -> list[str]:
        return sorted({edit.file for edit in self.edits})


RepairFunction = Callable[[int, Sequence[TestResult], Project], "RepairProposal | None"]


@dataclass
class PipelineRequest:
    """Everything one run is asked to do."""

    project_dir: Path
    mode: Literal["component", "circuit"] = "component"
    document_paths: list[Path] = field(default_factory=list)
    part_identity: PartIdentity | None = None
    use_profile: str = "Power and I/O sequencing"
    scope: str | None = None
    test_ids: list[str] | None = None
    allow_remote: bool = False
    provider: str | None = None
    max_repair_iterations: int = 3
    export_dir: Path | None = None
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.document_paths = [Path(path) for path in self.document_paths]
        if self.export_dir is not None:
            self.export_dir = Path(self.export_dir)
        if self.max_repair_iterations < 0:
            raise ValueError(
                f"max_repair_iterations must be >= 0, got {self.max_repair_iterations}"
            )


@dataclass
class StageProgress:
    """One stage's outcome, as the worker protocol and the GUI consume it."""

    stage: Stage
    status: Status
    detail: str
    elapsed_s: float = 0.0
    test_counts: dict[str, int] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    """The whole run: per-stage statuses, results, findings, and what was written."""

    status: Status
    stages: list[StageProgress]
    results: list[TestResult]
    findings: list[Finding]
    review_items: list[ReviewItem]
    artifacts: list[str]
    manifest_path: Path | None
    export_dir: Path | None
    diagnostics: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# repair enforcement


def check_repair(project: Project, proposal: RepairProposal) -> list[str]:
    """Return the project-relative paths a proposal may write, or raise.

    The two rules are deliberately redundant: the declared ``kind`` names the
    intent (so a tolerance relaxation or a test deletion is refused even when it
    is dressed up as a model tweak), and the resolved target must be inside
    ``models/candidates/<n>/`` (so a repair can never touch the circuit,
    evidence, tests, or the baseline).
    """
    if not proposal.edits:
        raise RepairViolation(f"repair proposal {proposal.description!r} contains no edits")
    candidates_root = project.root.joinpath(*_CANDIDATES_DIR)
    checked: list[str] = []
    for edit in proposal.edits:
        if edit.kind not in _REPAIR_KINDS:
            raise RepairViolation(
                f"unknown repair kind {edit.kind!r} in {edit.file}:{edit.path}; "
                f"expected one of {list(_REPAIR_KINDS)}"
            )
        refusal = _FORBIDDEN_REPAIR_KINDS.get(edit.kind)
        if refusal is not None:
            raise RepairViolation(f"{edit.kind}: {refusal} ({edit.file}:{edit.path})")
        if edit.old == edit.new:
            raise RepairViolation(f"repair edit {edit.file}:{edit.path} changes nothing")
        try:
            target = resolve_within(project.root, edit.file)
        except PathGuardError as exc:
            raise RepairViolation(f"repair target {edit.file!r} is not usable: {exc}") from exc
        if not target.is_relative_to(candidates_root):
            raise RepairViolation(
                f"repair may only write under models/candidates/<n>/: {edit.file} "
                f"(resolved to {target})"
            )
        checked.append(edit.file)
    return sorted(set(checked))


def apply_repair(project: Project, proposal: RepairProposal) -> list[str]:
    """Apply a proposal after :func:`check_repair`, returning the files written."""
    check_repair(project, proposal)
    written: list[str] = []
    for edit in proposal.edits:
        target = resolve_within(project.root, edit.file)
        if edit.old == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.new, encoding="utf-8", newline="\n")
            written.append(edit.file)
            continue
        if not target.is_file():
            raise RepairViolation(
                f"repair target {edit.file} does not exist; a repair edits candidate files, "
                "it does not create project artifacts"
            )
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(edit.old)
        if occurrences != 1:
            raise RepairViolation(
                f"repair anchor {edit.old!r} occurs {occurrences} time(s) in {edit.file}; "
                "refusing an ambiguous edit"
            )
        target.write_text(text.replace(edit.old, edit.new, 1), encoding="utf-8", newline="\n")
        written.append(edit.file)
    return sorted(set(written))


# --------------------------------------------------------------------------- #
# run bookkeeping


@dataclass
class _Run:
    request: PipelineRequest
    cancel: threading.Event | None
    deadline: float | None
    project: Project | None = None
    extraction: ExtractionResult | None = None
    requirements: dict[str, Requirement] = field(default_factory=dict)
    cases: list[TestCase] = field(default_factory=list)
    run_artifacts: list[RunArtifacts] = field(default_factory=list)
    results: list[TestResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)
    model_id: str | None = None
    capability: ModelCapability | None = None
    capability_gate: dict[str, str] | None = None
    baseline_version: int = 0
    provider_detail: str = ""
    export_dir: Path | None = None
    manifest_path: Path | None = None
    diagnostics: dict[str, str] = field(default_factory=dict)
    stage_artifacts: dict[Stage, list[str]] = field(default_factory=dict)

    @property
    def project_root(self) -> Path:
        if self.project is None:
            raise ProjectError("the project has not been identified yet")
        return self.project.root

    def remaining_s(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()


def _worst(statuses: Sequence[Status]) -> Status:
    if not statuses:
        return Status.PASS
    return max(statuses, key=lambda status: _SEVERITY[status])


def _counts(results: Sequence[TestResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    return counts


def _repair_pending(results: Sequence[TestResult]) -> list[TestResult]:
    """Results a repair step could act on (a BLOCKED run is not repairable here)."""
    return [result for result in results if result.status in (Status.FAIL, Status.UNKNOWN)]


def _relative(project: Project, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return path


# --------------------------------------------------------------------------- #
# controller


class PipelineController:
    """Runs the stage chain for one :class:`PipelineRequest`."""

    def __init__(self, config: AppConfig | None = None, *, repair: RepairFunction | None = None):
        self.config = config if config is not None else load_config()
        self.repair = repair

    # ------------------------------------------------------------------ run

    def run(
        self,
        request: PipelineRequest,
        progress: Callable[[StageProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> PipelineResult:
        """Execute every stage and return the run's observable outcome."""
        started = time.monotonic()
        state = _Run(
            request=request,
            cancel=cancel,
            deadline=None if request.deadline_s is None else started + request.deadline_s,
        )
        stages: list[StageProgress] = []
        fatal: str | None = None

        for stage in STAGE_ORDER:
            stage_started = time.monotonic()
            if fatal is not None:
                stages.append(
                    self._emit(progress, stage, Status.NOT_APPLICABLE, fatal, stage_started)
                )
                continue
            if cancel is not None and cancel.is_set():
                fatal = "not reached: cancelled"
                stages.append(
                    self._emit(
                        progress,
                        stage,
                        Status.BLOCKED,
                        "cancelled before the stage ran",
                        stage_started,
                    )
                )
                continue
            remaining = state.remaining_s()
            if remaining is not None and remaining <= 0:
                fatal = "not reached: deadline exhausted"
                stages.append(
                    self._emit(
                        progress,
                        stage,
                        Status.BLOCKED,
                        f"deadline_exceeded: the {request.deadline_s:g} s budget was exhausted",
                        stage_started,
                    )
                )
                continue

            try:
                status, detail, counts, artifacts = self._HANDLERS[stage](self, state)
            except RepairViolation as exc:
                status, detail, counts, artifacts = (
                    Status.UNKNOWN,
                    f"repair_violation: {exc}",
                    {},
                    [],
                )
                state.findings.append(
                    Finding(
                        code="repair_violation",
                        status=Status.UNKNOWN,
                        message=str(exc),
                        detail={"stage": stage.value},
                    )
                )
            except ProviderError as exc:
                status = Status.BLOCKED
                detail, counts, artifacts = f"{exc.code}: {exc.detail}", {}, []
            except ProjectError as exc:
                status, detail, counts, artifacts = Status.BLOCKED, f"project_error: {exc}", {}, []
            except DocumentStoreError as exc:
                status = Status.BLOCKED
                detail, counts, artifacts = f"document_store_error: {exc}", {}, []
            except OSError as exc:
                status = Status.BLOCKED
                detail, counts, artifacts = f"io_error: {type(exc).__name__}: {exc}", {}, []

            state.stage_artifacts[stage] = list(artifacts)
            stage_progress = self._emit(
                progress, stage, status, detail, stage_started, counts, artifacts
            )
            stages.append(stage_progress)
            if status in (Status.FAIL, Status.BLOCKED) and stage in _FATAL_STAGES:
                fatal = f"not reached: {stage.value} reported {status.value}"

        status = _worst([stage.status for stage in stages])
        artifacts = sorted({path for paths in state.stage_artifacts.values() for path in paths})
        diagnostics = dict(state.diagnostics)
        diagnostics.setdefault("provider", state.provider_detail or "not selected")
        if state.extraction is not None:
            diagnostics["cache_hits"] = str(state.extraction.cache_hits)
            diagnostics["disclosures"] = str(len(state.extraction.disclosures))
            diagnostics["provider_identity"] = state.extraction.provider_identity.provider
        if state.model_id:
            diagnostics["model_id"] = state.model_id
        if state.baseline_version:
            diagnostics["baseline_version"] = str(state.baseline_version)
        diagnostics["stages"] = ",".join(
            f"{stage.stage.value}={stage.status.value}" for stage in stages
        )
        diagnostics["elapsed_s"] = f"{time.monotonic() - started:.3f}"

        return PipelineResult(
            status=status,
            stages=stages,
            results=list(state.results),
            findings=list(state.findings),
            review_items=list(state.review_items),
            artifacts=artifacts,
            manifest_path=state.manifest_path,
            export_dir=state.export_dir,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _emit(
        progress: Callable[[StageProgress], None] | None,
        stage: Stage,
        status: Status,
        detail: str,
        started: float,
        counts: dict[str, int] | None = None,
        artifacts: Sequence[str] = (),
    ) -> StageProgress:
        stage_progress = StageProgress(
            stage=stage,
            status=status,
            detail=detail,
            elapsed_s=round(time.monotonic() - started, 6),
            test_counts=dict(counts or {}),
            artifacts=list(artifacts),
        )
        if progress is not None:
            progress(stage_progress)
        return stage_progress

    # -------------------------------------------------------------- stages

    def _stage_identify(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Open the project and record the requested identity/mode."""
        request = state.request
        root = Path(request.project_dir)
        if not (root / PROJECT_FILENAME).is_file():
            return (
                Status.BLOCKED,
                f"project_not_found: {root} has no {PROJECT_FILENAME}; "
                "create the project before running it",
                {},
                [],
            )
        project = Project(root)
        state.project = project
        config = project.config
        updated: list[str] = []
        if request.part_identity is not None and config.part != request.part_identity:
            config.part = request.part_identity
            updated.append("part_identity")
        if config.mode != request.mode:
            config.mode = request.mode
            updated.append("mode")
        if config.use_profile != request.use_profile:
            config.use_profile = request.use_profile
            updated.append("use_profile")
        artifacts: list[str] = []
        if updated:
            (root / PROJECT_FILENAME).write_text(
                config.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            artifacts.append(PROJECT_FILENAME)
        part = config.part
        detail = (
            f"project={config.project_id} mode={config.mode} use_profile={config.use_profile!r} "
            f"documents={len(project.documents())}"
        )
        if part is not None:
            detail += f" part={part.base_part or part.ordering_code or 'unresolved'}"
            detail += f" confidence={part.confidence}"
        if updated:
            detail += f" updated={','.join(updated)}"
        return Status.PASS, detail, {}, artifacts

    def _stage_collect_evidence(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Ingest the request's documents, then run the extraction provider."""
        project = self._project(state)
        request = state.request
        artifacts: list[str] = []
        store = DocumentStore(project.root)
        for path in request.document_paths:
            try:
                record = store.add_file(
                    path,
                    doc_type="other",
                    provenance="user_supplied",
                    classification="unknown",
                    remote_inference_allowed=False,
                )
            except (OSError, ValueError, DocumentStoreError) as exc:
                return (
                    Status.BLOCKED,
                    f"document_unreadable: {path}: {type(exc).__name__}: {exc}",
                    {},
                    artifacts,
                )
            artifacts.append(f"docs/{record.doc_id}.json")

        try:
            selection = select_provider(
                self.config, requested=request.provider, allow_bob_shell=False
            )
        except ProviderError as exc:
            return Status.BLOCKED, f"{exc.code}: {exc.detail}", {}, artifacts
        state.provider_detail = selection.detail

        try:
            extraction = extract_requirements(
                project,
                provider=selection.provider,
                policy=self.config.data_policy,
                allow_remote=request.allow_remote,
                cancel=state.cancel,
            )
        except ProviderError as exc:
            declared = project.read_requirements_file()
            if not declared:
                return Status.BLOCKED, f"{exc.code}: {exc.detail}", {}, artifacts
            # A project that already declares its requirement set (a hand-authored
            # circuit project) can still be evaluated; the stage says so instead of
            # pretending the extraction happened.
            return (
                Status.UNKNOWN,
                f"{exc.code}: {exc.detail}; continuing with the {len(declared)} requirement(s) "
                "declared in the project",
                {},
                artifacts,
            )
        state.extraction = extraction
        state.findings.extend(extraction.findings)
        artifacts.extend(self._write_extraction(project, extraction))
        detail = f"{extraction.detail}; provider_selection={selection.detail}"
        return Status.PASS, detail, {}, artifacts

    def _write_extraction(self, project: Project, extraction: ExtractionResult) -> list[str]:
        """Persist what extraction produced, including its open questions."""
        evidence = project.path("evidence")
        identity = extraction.provider_identity
        paths: list[str] = []
        paths.append(
            _relative(
                project,
                _write_json(
                    evidence / "requirements.extracted.json",
                    {
                        "project_id": project.config.project_id,
                        "provider": json.loads(identity.model_dump_json()),
                        "requirements": [
                            json.loads(r.model_dump_json(by_alias=True))
                            for r in extraction.requirements
                        ],
                    },
                ),
            )
        )
        paths.append(
            _relative(
                project,
                _write_json(
                    evidence / "pinmap.json",
                    {
                        "project_id": project.config.project_id,
                        "documents": [
                            {"doc_id": record.doc_id, "file_hash": record.file_hash}
                            for record in project.documents()
                        ],
                        "pins": [json.loads(pin.model_dump_json()) for pin in extraction.pins],
                    },
                ),
            )
        )
        if extraction.part is not None:
            paths.append(
                _relative(
                    project,
                    _write_json(
                        evidence / "identity.json",
                        json.loads(extraction.part.model_dump_json()),
                    ),
                )
            )
        paths.append(
            _relative(
                project,
                _write_json(
                    evidence / "capability_summary.json",
                    {
                        "note": (
                            "reporting material only — this is what the documents say, not a "
                            "ModelCapability record; a capability claim requires a probe"
                        ),
                        "provider": identity.provider,
                        "behaviors": dict(sorted(extraction.capability_summary.items())),
                    },
                ),
            )
        )
        paths.append(
            _relative(
                project,
                _write_json(
                    project.path(_EXTRACTION_FILE),
                    {
                        "project_id": project.config.project_id,
                        "provider": json.loads(identity.model_dump_json()),
                        "detail": extraction.detail,
                        "cache_hits": extraction.cache_hits,
                        "disclosures": [
                            json.loads(item.model_dump_json()) for item in extraction.disclosures
                        ],
                        "findings": [
                            json.loads(item.model_dump_json()) for item in extraction.findings
                        ],
                        "issues": [
                            {
                                "code": issue.code,
                                "severity": issue.severity,
                                "req_id": issue.req_id,
                                "message": issue.message,
                                "detail": issue.detail,
                            }
                            for issue in extraction.issues
                        ],
                    },
                ),
            )
        )
        return paths

    def _stage_build_requirements(
        self, state: _Run
    ) -> tuple[Status, str, dict[str, int], list[str]]:
        """Apply the review's honest defaults and publish the requirement set."""
        project = self._project(state)
        extraction = state.extraction
        if extraction is None:
            return Status.UNKNOWN, "no_extraction: no evidence was collected", {}, []
        requirements = apply_review(extraction.requirements, extraction.review)
        state.requirements = {requirement.req_id: requirement for requirement in requirements}
        path = project.requirements_path
        _write_json(
            path,
            {
                "project_id": project.config.project_id,
                "provider": extraction.provider_identity.provider,
                "requirements": [
                    json.loads(requirement.model_dump_json(by_alias=True))
                    for requirement in requirements
                ],
            },
        )
        unverified = sum(1 for r in requirements if not r.citation_verified)
        detail = (
            f"{len(requirements)} requirement(s) written to "
            f"{_relative(project, path)}; citation_verified=False for {unverified}"
        )
        artifacts = [_relative(project, path)]
        if not requirements:
            return (
                Status.UNKNOWN,
                f"no_requirements: {detail}",
                {},
                artifacts,
            )
        return Status.PASS, detail, {}, artifacts

    def _stage_review_sources(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Publish the review questions, keeping already-answered ones."""
        project = self._project(state)
        items = _merge_review_items(project.review_items(), self._review_items(state))
        state.review_items = items
        path = project.review_path
        _write_json(path, {"items": [json.loads(item.model_dump_json()) for item in items]})
        open_blocking = [item for item in items if item.blocking and not item.resolution]
        detail = (
            f"{len(items)} review item(s), {len(open_blocking)} blocking and unanswered "
            f"-> {_relative(project, path)}"
        )
        artifacts = [_relative(project, path)]
        if open_blocking:
            codes = ", ".join(sorted({item.kind for item in open_blocking}))
            return Status.UNKNOWN, f"{detail}; blocking kinds: {codes}", {}, artifacts
        return Status.PASS, detail, {}, artifacts

    def _review_items(self, state: _Run) -> list[ReviewItem]:
        if state.extraction is None:
            return []
        return list(state.extraction.review.items)

    def _stage_freeze_baseline(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Freeze requirements + tests before any repair, recording a revision."""
        project = self._project(state)
        request = state.request
        requirements = project.read_requirements_file()
        # Freeze the tests this run selected (scope/test_ids), not merely the
        # declared file: that is what makes a scope change a visible revision, and
        # it keeps the frozen set equal to the set the run was judged against.
        tests = project.tests(scope=request.scope, test_ids=request.test_ids)
        declared = len(project.read_tests_file())
        selected_note = (
            f"tests selected: {len(tests)} of {declared} declared"
            + (f" (scope={request.scope!r})" if request.scope else "")
            + (f" (test_ids={sorted(request.test_ids)})" if request.test_ids else "")
        )
        existing = project.baseline()
        version = 1
        if existing is not None:
            version = max(1, existing.baseline_version)
        provisional = Baseline(
            baseline_version=version,
            requirements=requirements,
            tests=tests,
        )
        content_hash = provisional.compute_hash()
        test_hash = sha256_bytes(
            canonical_json_bytes([json.loads(case.model_dump_json()) for case in tests])
        )
        revision = existing is not None and (
            existing.requirement_baseline_hash != content_hash or existing.test_hash != test_hash
        )
        if revision:
            version = max(1, existing.baseline_version) + 1

        baseline = Baseline(
            baseline_version=version,
            frozen_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            requirements=requirements,
            tests=tests,
            notes=[
                f"frozen before repair; requirement_baseline_hash={content_hash}",
                f"requirements={len(requirements)} tests={len(tests)}",
                selected_note,
            ],
        )
        baseline.requirement_baseline_hash = content_hash
        baseline.test_hash = test_hash
        # A repeated run freezes the same baseline, including its original stamp.
        # Rewriting an unchanged baseline made repair-preservation checks depend
        # on whether both runs happened within the same wall-clock second.
        if existing is None or revision:
            project.write_baseline(baseline)
        state.baseline_version = version
        state.requirements = {requirement.req_id: requirement for requirement in requirements}

        artifacts = [BASELINE_FILENAME]
        detail = (
            f"baseline v{version} frozen ({len(requirements)} requirement(s), {len(tests)} test(s), "
            f"hash {content_hash[:12]})"
        )
        if revision:
            assert existing is not None
            item = ReviewItem(
                id=f"RV_baseline_v{version}",
                kind="ambiguity",
                question=(
                    f"The requirement/test set changed since baseline v{existing.baseline_version} "
                    f"({existing.requirement_baseline_hash[:12]} -> {content_hash[:12]}). "
                    "Is the new set the one to judge against?"
                ),
                options=[
                    "accept the new baseline",
                    "restore the previous requirement/test files",
                    "reject the change",
                ],
                recommended="accept the new baseline",
                blocking=False,
                affected_requirement_ids=[],
            )
            items = _merge_review_items(project.review_items(), [item])
            state.review_items = items
            _write_json(
                project.review_path,
                {"items": [json.loads(entry.model_dump_json()) for entry in items]},
            )
            artifacts.append(_REVIEW_FILE)
            detail += f"; baseline_version bumped and {item.id} added to review.json"
        return Status.PASS, detail, {}, artifacts

    def _stage_select_model(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Pick the model artifact the run will claim, and its declared capability."""
        project = self._project(state)
        store = ModelStore(project.root)
        records = store.records()
        capabilities = _load_capabilities(project)
        model_id: str | None = None
        capability: ModelCapability | None = None
        if records:
            preferred = _preferred_record(records)
            model_id = preferred.model_id
            capability = next((item for item in capabilities if item.model_id == model_id), None)
        elif capabilities:
            capability = capabilities[0]
            model_id = capability.model_id

        gate: dict[str, str] | None = None
        behavior_map_path = project.root / "evidence" / "capability_map.json"
        if capabilities and behavior_map_path.is_file():
            raw = json.loads(behavior_map_path.read_text(encoding="utf-8"))
            behavior_map = {str(key): str(value) for key, value in raw.items()}
            gate = gate_from_capability(capabilities, behavior_map) or None

        state.model_id = model_id
        state.capability = capability
        state.capability_gate = gate
        if model_id is None:
            return (
                Status.UNKNOWN,
                "no_model_available: no model record under models/records/ and no capability "
                "record under models/capabilities/",
                {},
                [],
            )
        detail = f"model_id={model_id} records={len(records)} capabilities={len(capabilities)}"
        if capability is not None:
            detail += f" evidence_level={capability.evidence_level.value}"
        if gate:
            detail += f" gated_requirements={len(gate)}"
        return Status.PASS, detail, {}, []

    def _stage_compile_simulate(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Run every selected case in its own directory (never judging it here)."""
        project = self._project(state)
        request = state.request
        cases = project.tests(scope=request.scope, test_ids=request.test_ids)
        state.cases = cases
        if not cases:
            return Status.NOT_APPLICABLE, "no test case matches the request", {}, []
        context = self._run_context(state)
        artifacts = run_deck_tests(context, cases, state.cancel)
        state.run_artifacts = artifacts
        paths = [
            f"runs/{artifact.run_id}/deck.cir"
            for artifact in artifacts
            if artifact.deck_path is not None
        ]
        usable = [artifact for artifact in artifacts if artifact.usability.usable]
        blocked = [artifact for artifact in artifacts if artifact.blocked_reason]
        counts = {"PASS": len(usable), "BLOCKED": len(blocked)}
        if context.ltspice is None:
            return (
                Status.BLOCKED,
                f"simulator_unavailable: no LTspice executable is configured or installed; "
                f"{len(artifacts)} deck(s) were written and refused",
                counts,
                paths,
            )
        if blocked:
            reasons = ", ".join(
                sorted({artifact.blocked_reason or "unknown" for artifact in blocked})
            )
            return (
                Status.BLOCKED,
                f"{len(blocked)}/{len(artifacts)} run(s) unusable: {reasons}",
                counts,
                paths,
            )
        return Status.PASS, f"{len(usable)}/{len(artifacts)} run(s) usable", counts, paths

    def _stage_evaluate(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Turn every run into a TestResult (the verdicts live in the engine)."""
        project = self._project(state)
        if not state.run_artifacts:
            return Status.NOT_APPLICABLE, "no run to evaluate", {}, []
        results = self._evaluate(state, state.cases, state.run_artifacts)
        state.results = results
        paths = [self._write_results(state, project, results)]
        counts = _counts(results)
        status = _worst([result.status for result in results])
        detail = f"{len(results)} result(s): {counts}"
        if status is Status.NOT_APPLICABLE:
            detail += " (every case was not applicable)"
        return status, detail, counts, paths

    def _evaluate(
        self, state: _Run, cases: Sequence[TestCase], artifacts: Sequence[RunArtifacts]
    ) -> list[TestResult]:
        project = self._project(state)
        requirements = state.requirements or project.requirements()
        return [
            evaluate_case(
                case,
                artifact,
                requirements,
                supply_domains=project.config.supply_domains,
                capability_gate=state.capability_gate,
            )
            for case, artifact in zip(cases, artifacts, strict=True)
        ]

    def _write_results(self, state: _Run, project: Project, results: Sequence[TestResult]) -> str:
        latest: dict[str, RunArtifacts] = {}
        for artifact in state.run_artifacts:
            latest[artifact.test_id] = artifact
        for result in results:
            artifact = latest.get(result.test_id)
            if artifact is not None:
                _write_json(
                    artifact.run_dir / _RESULTS_FILE,
                    json.loads(result.model_dump_json()),
                )
        payload = {
            "project_id": project.config.project_id,
            "baseline_version": state.baseline_version,
            "summary": _counts(results),
            "results": [json.loads(result.model_dump_json()) for result in results],
        }
        return _relative(project, _write_json(project.root / _RESULTS_FILE, payload))

    def _stage_repair(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Bounded, baseline-safe repair loop (see the module docstring)."""
        request = state.request
        if request.max_repair_iterations <= 0:
            return Status.NOT_APPLICABLE, "repair is disabled (max_repair_iterations=0)", {}, []
        if not state.results:
            return Status.NOT_APPLICABLE, "no evaluated result needs repair", {}, []
        pending = _repair_pending(state.results)
        if not pending:
            if all(result.status is Status.BLOCKED for result in state.results):
                reasons = ", ".join(
                    sorted({result.blocked_reason or "unknown" for result in state.results})
                )
                return (
                    Status.NOT_APPLICABLE,
                    f"every run is BLOCKED ({reasons}); repair cannot proceed without a simulation",
                    {},
                    [],
                )
            return Status.NOT_APPLICABLE, "no result needs repair", {}, []
        project = self._project(state)
        artifacts: list[str] = []

        def finish(status: Status, detail: str) -> tuple[Status, str, dict[str, int], list[str]]:
            artifacts.append(self._write_results(state, project, state.results))
            return status, detail, _counts(state.results), artifacts

        if self.repair is None:
            return (
                Status.UNKNOWN,
                f"repair_not_configured: {len(pending)} result(s) need repair but no repair step "
                "is configured",
                {},
                [],
            )

        applied = 0
        pending_count = len(pending)
        for iteration in range(1, request.max_repair_iterations + 1):
            if state.cancel is not None and state.cancel.is_set():
                return Status.BLOCKED, "cancelled during repair", {}, artifacts
            proposal = self.repair(iteration, list(state.results), project)
            if proposal is None:
                break
            written = apply_repair(project, proposal)  # raises RepairViolation
            applied = iteration
            artifacts.extend(written)
            artifacts.append(self._log_repair(project, iteration, proposal, written))
            improved = self._rerun_after_repair(state, iteration)
            if not improved:
                return finish(
                    Status.UNKNOWN,
                    f"repair iteration {iteration} was applied but not re-verified (no simulator "
                    "ran), so no improvement can be claimed",
                )
            after_count = len(_repair_pending(state.results))
            if after_count >= pending_count:
                return finish(
                    Status.UNKNOWN,
                    f"repair iteration {iteration} did not improve the results "
                    f"({after_count} result(s) still need repair); further candidates are needed",
                )
            pending_count = after_count
            if pending_count == 0:
                return finish(
                    Status.PASS,
                    f"{applied} repair iteration(s) applied; no evaluated result needs repair "
                    "any more",
                )
        if applied == 0:
            return Status.NOT_APPLICABLE, "the repair step proposed nothing", {}, artifacts
        return finish(
            Status.UNKNOWN,
            f"repair_cap_reached: {applied} iteration(s) (= max_repair_iterations) applied and "
            f"{pending_count} result(s) still need repair",
        )

    def _rerun_after_repair(self, state: _Run, iteration: int) -> bool:
        """Re-run and re-evaluate the cases that needed repair; False when not possible."""
        needing = {
            result.test_id
            for result in state.results
            if result.status in (Status.FAIL, Status.UNKNOWN)
        }
        cases = [case for case in state.cases if case.test_id in needing]
        if not cases:
            return True
        context = self._run_context(state)
        if context.ltspice is None:
            state.diagnostics["repair_rerun"] = "not attempted: no LTspice executable"
            return False
        artifacts = run_deck_tests(context, cases, state.cancel, run_id_prefix=f"repair{iteration}")
        state.run_artifacts = [*state.run_artifacts, *artifacts]
        replaced = self._evaluate(state, cases, artifacts)
        by_id = {result.test_id: result for result in state.results}
        for result in replaced:
            by_id[result.test_id] = result
        state.results = [by_id.get(result.test_id, result) for result in state.results] + [
            result
            for result in replaced
            if result.test_id not in {r.test_id for r in state.results}
        ]
        return True

    def _log_repair(
        self, project: Project, iteration: int, proposal: RepairProposal, written: Sequence[str]
    ) -> str:
        directory = _repair_directory(project, proposal)
        path = directory / "repair_log.json"
        existing: list[object] = []
        if path.is_file():
            try:
                existing = list(json.loads(path.read_text(encoding="utf-8")).get("iterations", []))
            except json.JSONDecodeError, AttributeError:
                existing = []
        existing.append(
            {
                "iteration": iteration,
                "description": proposal.description,
                "applied_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": list(written),
                "edits": [
                    {
                        "file": edit.file,
                        "path": edit.path,
                        "kind": edit.kind,
                        "old": edit.old,
                        "new": edit.new,
                    }
                    for edit in proposal.edits
                ],
            }
        )
        _write_json(path, {"iterations": existing})
        return _relative(project, path)

    def _stage_export(self, state: _Run) -> tuple[Status, str, dict[str, int], list[str]]:
        """Write the portable export (the only place receipts are computed)."""
        request = state.request
        if request.export_dir is None:
            return Status.NOT_APPLICABLE, "no export directory was requested", {}, []
        project = self._project(state)
        try:
            exported = export_project(
                project,
                Path(request.export_dir),
                model_id=state.model_id,
                requirements=list(state.requirements.values()) or None,
                tests=list(state.cases) or None,
                results=list(state.results) or None,
                capability=state.capability,
                documents=project.documents(),
                parameters={
                    "mode": project.config.mode,
                    "use_profile": project.config.use_profile,
                },
                reproduction=["boardmodeler circuit check --project . --circuit circuit/demo.asc"],
            )
        except Exception as exc:  # an export that cannot be produced is BLOCKED, not PASS
            return (
                Status.BLOCKED,
                f"export_failed: {type(exc).__name__}: {exc}",
                {},
                [],
            )
        state.export_dir = Path(request.export_dir)
        state.manifest_path = exported.manifest_path
        state.findings.extend(exported.findings)
        paths = exported.relative_files()
        failures = [finding for finding in exported.findings if finding.status is Status.FAIL]
        detail = f"{len(exported.files)} file(s) written to {state.export_dir}"
        if failures:
            codes = ", ".join(sorted({finding.code for finding in failures}))
            return Status.FAIL, f"{detail}; export findings: {codes}", {}, paths
        return Status.PASS, detail, {}, paths

    # ------------------------------------------------------------ helpers

    def _project(self, state: _Run) -> Project:
        if state.project is None:
            raise ProjectError("the project was not identified")
        return state.project

    def _run_context(self, state: _Run) -> RunContext:
        project = self._project(state)
        ltspice_config = self.config.ltspice
        install = locate(ltspice_config.path)
        lib_dir = Path(ltspice_config.lib_dir) if ltspice_config.lib_dir else default_lib_dir()
        timeout_s = ltspice_config.timeout_s
        remaining = state.remaining_s()
        if remaining is not None:
            timeout_s = max(1.0, min(timeout_s, remaining))
        return RunContext(
            project_dir=project.root,
            ltspice=install.path if install is not None else None,
            timeout_s=timeout_s,
            extra_switches=tuple(ltspice_config.extra_switches),
            ltspice_lib_dir=lib_dir,
        )

    _HANDLERS: dict[
        Stage, Callable[[PipelineController, _Run], tuple[Status, str, dict[str, int], list[str]]]
    ] = {}


def _merge_review_items(
    existing: Sequence[ReviewItem], new: Sequence[ReviewItem]
) -> list[ReviewItem]:
    """Existing items first, then unseen ones, keyed by id (stable and idempotent)."""
    merged = list(existing)
    seen = {item.id for item in merged}
    for item in new:
        if item.id in seen:
            continue
        seen.add(item.id)
        merged.append(item)
    return merged


def _preferred_record(records: Sequence[object]) -> object:
    generated = [record for record in records if getattr(record, "kind", "") != "vendor_original"]
    return generated[0] if generated else records[0]


def _load_capabilities(project: Project) -> list[ModelCapability]:
    directory = project.root / "models" / "capabilities"
    if not directory.is_dir():
        return []
    return [
        ModelCapability.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def _repair_directory(project: Project, proposal: RepairProposal) -> Path:
    """The ``models/candidates/<n>/`` directory a proposal writes into."""
    for edit in proposal.edits:
        parts = Path(edit.file).parts
        if len(parts) >= 3 and parts[:2] == _CANDIDATES_DIR:
            return resolve_within(project.root, Path(*parts[:3]))
    return resolve_within(project.root, Path(*_CANDIDATES_DIR))


PipelineController._HANDLERS = {
    Stage.IDENTIFY: PipelineController._stage_identify,
    Stage.COLLECT_EVIDENCE: PipelineController._stage_collect_evidence,
    Stage.BUILD_REQUIREMENTS: PipelineController._stage_build_requirements,
    Stage.REVIEW_SOURCES: PipelineController._stage_review_sources,
    Stage.FREEZE_BASELINE: PipelineController._stage_freeze_baseline,
    Stage.SELECT_MODEL: PipelineController._stage_select_model,
    Stage.COMPILE_SIMULATE: PipelineController._stage_compile_simulate,
    Stage.EVALUATE: PipelineController._stage_evaluate,
    Stage.REPAIR: PipelineController._stage_repair,
    Stage.EXPORT: PipelineController._stage_export,
}
