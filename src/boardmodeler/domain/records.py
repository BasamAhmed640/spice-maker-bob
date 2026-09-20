"""Serializable records (D4).

Every model here is the shared contract between the CLI, the GUI, the pipeline,
the export, and the tests. Rules that apply to all of them:

* ``extra="forbid"`` — a typo in a fixture is an error, not silently dropped.
* ``schema_version`` is stamped from :data:`SCHEMA_VERSION`.
* Round-trip through ``model_dump_json(indent=2)`` → ``model_validate_json`` is
  lossless, so baselines can be hashed and compared byte-for-byte.

Statuses are honest by construction: a model capability that was not probed is
``not_tested``/``unknown``, and a test result may only carry ``PASS`` when the
producing run left an artifact (enforced by the engine, documented here).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boardmodeler.domain.enums import (
    Criticality,
    EvidenceExtraction,
    EvidenceLevel,
    ModelKind,
    ProviderKind,
    ReaderBackend,
    RequirementClass,
    RequirementKind,
    RequirementOrigin,
    Status,
)
from boardmodeler.domain.expressions import Expr

__all__ = [
    "BEHAVIOR_KEYS",
    "SCHEMA_VERSION",
    "AbstractionBoundary",
    "ArchiveEntry",
    "CircuitMapping",
    "Condition",
    "DataDisclosure",
    "DocumentRecord",
    "EvidenceRef",
    "ExpectationSpec",
    "Finding",
    "HierarchyBlock",
    "Limit",
    "MappingEntry",
    "ModelCapability",
    "PageRef",
    "PartIdentity",
    "PinDefinition",
    "ProviderIdentity",
    "Requirement",
    "ReviewItem",
    "RunManifest",
    "Stimulus",
    "TestCase",
    "TestResult",
]

SCHEMA_VERSION = 2

BEHAVIOR_KEYS: tuple[str, ...] = (
    "startup",
    "shutdown",
    "dc_regulation",
    "load_transients",
    "input_current",
    "current_limit_recovery",
    "switching_waveforms",
    "compensation_loop",
    "thermal_dependence",
    "reverse_current_prebias",
)
"""The exact behavior keys a :class:`ModelCapability` must report on.

Missing keys are rejected so that an unexamined behavior cannot be silently
omitted — in particular ``thermal_dependence``, which BoardModeler does not
model and must therefore always appear as ``unsupported``.
"""


class Record(BaseModel):
    """Base class for every serialized record."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``TestCase``/``TestResult`` are data schemas, not pytest test classes:
    # mark them so importing them into a test module never triggers collection.
    __test__ = False

    schema_version: int = SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# evidence


class PageRef(Record):
    """A page in a document: 0-based PDF page plus the printed label if known."""

    pdf_page: int = Field(ge=0)
    printed_label: str | None = None


class EvidenceRef(Record):
    """A verbatim excerpt and where it came from.

    ``excerpt`` is deliberately capped at 400 characters: it is a citation, not
    a reproduction. ``citation_verified`` on the owning :class:`Requirement` is
    only true when the excerpt was found in the extracted text of the cited
    page.
    """

    doc_id: str
    page: PageRef | None = None
    section: str | None = None
    table: str | None = None
    figure: str | None = None
    excerpt: str = Field(max_length=400)
    extraction: EvidenceExtraction


class DocumentRecord(Record):
    """One input document, identified by content hash."""

    doc_id: str
    title: str
    manufacturer: str | None = None
    doc_type: Literal[
        "datasheet",
        "errata",
        "design_guide",
        "app_note",
        "user_guide",
        "vendor_model",
        "synthetic_contract",
        "other",
    ]
    revision: str | None = None
    issued_date: str | None = None
    file_hash: str
    source_url: str | None = None
    provenance: Literal["user_supplied", "downloaded_public", "synthetic_fixture"]
    classification: Literal["public", "internal", "confidential", "unknown"] = "unknown"
    remote_inference_allowed: bool = False
    page_count: int = Field(default=0, ge=0)
    page_labels: dict[int, str] = Field(default_factory=dict)
    text_extraction: Literal["embedded", "ocr", "hybrid", "none"] = "none"
    path: str = ""
    redistribution_allowed: bool = False


class PartIdentity(Record):
    """Best-effort identification of the device under study."""

    manufacturer: str | None = None
    family: str | None = None
    ordering_code: str | None = None
    base_part: str | None = None
    package: str | None = None
    revision: str | None = None
    ambiguities: list[str] = Field(default_factory=list)
    confidence: Literal["resolved", "partial", "unresolved"] = "unresolved"
    evidence: list[EvidenceRef] = Field(default_factory=list)


class PinDefinition(Record):
    """One physical pin of a device, grounded in a document."""

    part_id: str
    physical_pin: str
    name: str
    function: str
    polarity: Literal["active_high", "active_low", "bidirectional", "not_applicable"]
    direction: Literal["input", "output", "bidir", "power", "ground", "nc"]
    supply_domain: str | None = None
    output_topology: Literal[
        "open_drain", "push_pull", "tri_state", "power", "input_only", "unknown"
    ]
    connection_requirement: Literal["required", "optional", "no_connect", "conditional"]
    unused_pin_treatment: str | None = None
    behavior: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    mapped_symbol_pin: str | None = None


# --------------------------------------------------------------------------- #
# requirements


class RelativeLimit(Record):
    """An affine bound, in the limit's unit: factor * parameter + offset.

    The parameter is an explicitly cited operating-condition name, never a
    Python or SPICE expression. Evaluation requires an explicit operating point.
    """

    parameter: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    factor: float = Field(allow_inf_nan=False)
    offset: float = Field(default=0.0, allow_inf_nan=False)


class Limit(Record):
    """A numeric limit triple with a unit. Values must be ordered if all given."""

    min: float | None = None
    typ: float | None = None
    max: float | None = None
    unit: str
    min_relative: RelativeLimit | None = None
    typ_relative: RelativeLimit | None = None
    max_relative: RelativeLimit | None = None

    @model_validator(mode="after")
    def _check_order(self) -> Limit:
        for side in ("min", "typ", "max"):
            if getattr(self, side) is not None and getattr(self, side + "_relative") is not None:
                raise ValueError(f"{side} cannot be both a scalar and a relative bound")
        present = [v for v in (self.min, self.typ, self.max) if v is not None]
        if present != sorted(present):
            raise ValueError(f"limit min/typ/max out of order: {self.min}/{self.typ}/{self.max}")
        return self


class Condition(Record):
    """Operating condition a requirement applies under."""

    text: str
    parameter_overrides: dict[str, float] = Field(default_factory=dict)


class Requirement(Record):
    """A single falsifiable requirement with its provenance."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    req_id: str
    applies_to: str
    configuration: str | None = None
    kind: RequirementKind
    req_class: RequirementClass = Field(
        validation_alias="class", serialization_alias="class", repr=False
    )
    criticality: Criticality
    origin: RequirementOrigin
    statement: str
    limits: Limit | None = None
    expression: Expr | None = None
    conditions: list[Condition] = Field(default_factory=list)
    signal_refs: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    citation_verified: bool = False
    status: Literal["active", "superseded", "conflict"] = "active"


class ModelCapability(Record):
    """What a simulation model is actually known to reproduce.

    ``behaviors`` must carry exactly :data:`BEHAVIOR_KEYS`. A behavior whose
    probe did not exercise it stays ``unknown``/``not_tested``; capability
    probing never upgrades that to ``supported``.
    """

    model_id: str
    kind: ModelKind
    behaviors: dict[str, Literal["supported", "unsupported", "unknown", "not_tested"]] = Field(
        default_factory=dict
    )
    valid_domain: dict[str, Limit | str] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    evidence_level: EvidenceLevel
    probe_results: list[str] = Field(default_factory=list)
    source_model_hash: str

    @model_validator(mode="after")
    def _check_behavior_keys(self) -> ModelCapability:
        missing = [k for k in BEHAVIOR_KEYS if k not in self.behaviors]
        extra = [k for k in self.behaviors if k not in BEHAVIOR_KEYS]
        if missing or extra:
            raise ValueError(
                f"behaviors must be exactly the {len(BEHAVIOR_KEYS)} defined keys "
                f"(missing={missing}, extra={extra})"
            )
        return self


# --------------------------------------------------------------------------- #
# tests


class Stimulus(Record):
    """One applied stimulus in a test scenario."""

    kind: Literal[
        "dc_ramp",
        "dc_step",
        "pulse",
        "ac",
        "load_step",
        "rail_sequence",
        "clock",
        "external_drive",
        "open",
    ]
    target: str
    params: dict[str, float] = Field(default_factory=dict)


class ExpectationSpec(Record):
    """How a test's evaluator interprets its assertion results.

    ``satisfy`` — every assertion must hold.
    ``violate_detected`` — the run must complete *and* the violation must be
    observed; a run that fails to complete is FAIL, not PASS.
    """

    kind: Literal["satisfy", "violate_detected"]
    detail: str


class TestCase(Record):
    """One executable check bound to requirements."""

    test_id: str
    requirement_ids: list[str] = Field(default_factory=list)
    scenario_id: str
    scope: Literal[
        "model_qualification", "circuit_compliance", "fault_detection", "primitive_reference"
    ]
    stimulus: list[Stimulus] = Field(default_factory=list)
    deck_template: str
    expected: ExpectationSpec
    measurement: list[str] = Field(default_factory=list)
    max_timestep_s: float | None = None
    tolerance: dict[str, float] = Field(default_factory=dict)


class TestResult(Record):
    """Outcome of a test against one run.

    ``measured`` must be non-empty for PASS/FAIL/NOT_APPLICABLE-only rows are
    permitted to be empty. ``unknown_reason``/``blocked_reason`` are mandatory
    for the corresponding status so that an honest status always carries a
    human-readable justification.
    """

    test_id: str
    status: Status
    requirement_ids: list[str] = Field(default_factory=list)
    measured: dict[str, float | str] = Field(default_factory=dict)
    expected: str
    detail: str = ""
    waveform_refs: list[str] = Field(default_factory=list)
    log_ref: str | None = None
    run_id: str = ""
    duration_s: float = 0.0
    unknown_reason: str | None = None
    blocked_reason: str | None = None

    @model_validator(mode="after")
    def _check_reasons(self) -> TestResult:
        if self.status is Status.UNKNOWN and not self.unknown_reason:
            raise ValueError("UNKNOWN result requires unknown_reason")
        if self.status is Status.BLOCKED and not self.blocked_reason:
            raise ValueError("BLOCKED result requires blocked_reason")
        return self


# --------------------------------------------------------------------------- #
# circuit mapping


class MappingEntry(Record):
    """One placed component and the model that simulates it."""

    refdes: str
    model_id: str | None = None
    manufacturer: str | None = None
    part_number: str | None = None
    package: str | None = None
    value: str | None = None
    symbol_pin: dict[str, str] = Field(default_factory=dict)


class HierarchyBlock(Record):
    """A subcircuit instance and the node mapping at its boundary."""

    instance_refdes: str
    subckt: str
    node_order: list[str] = Field(default_factory=list)
    parent_nodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_arity(self) -> HierarchyBlock:
        if len(self.node_order) != len(self.parent_nodes):
            raise ValueError(
                f"{self.instance_refdes}: node_order ({len(self.node_order)}) and "
                f"parent_nodes ({len(self.parent_nodes)}) must have equal length"
            )
        return self


class AbstractionBoundary(Record):
    """Something deliberately not modeled, recorded rather than hidden."""

    refdes: str
    omitted_pins: list[str] = Field(default_factory=list)
    preserved_connectivity: bool
    reason: str


class CircuitMapping(Record):
    """How a source circuit was turned into something simulatable."""

    source: Literal["ltspice_asc", "spice_netlist", "neutral_csv"]
    entries: list[MappingEntry] = Field(default_factory=list)
    hierarchy: list[HierarchyBlock] = Field(default_factory=list)
    abstractions: list[AbstractionBoundary] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# manifests


class ArchiveEntry(Record):
    """One file recorded in a run or export manifest."""

    path: str
    sha256: str
    size: int = Field(ge=0)


class ProviderIdentity(Record):
    """Which extraction provider ran and in what native usage units.

    Usage is never converted between units (tokens vs turns vs requests); the
    unit is reported alongside the number.
    """

    provider: str
    kind: ProviderKind
    model: str | None = None
    endpoint: str | None = None
    usage_units: Literal["tokens", "turns", "requests", "none"] = "none"
    usage: dict[str, float] = Field(default_factory=dict)
    detail: dict[str, str] = Field(default_factory=dict)


class DataDisclosure(Record):
    """Exactly what was sent off-box, recorded before the first call."""

    provider: str
    endpoint: str | None = None
    doc_ids: list[str] = Field(default_factory=list)
    page_ranges: dict[str, list[int]] = Field(default_factory=dict)
    chars: int = Field(default=0, ge=0)
    policy: str


class RunManifest(Record):
    """Everything needed to say what a run was and what it did not do."""

    run_id: str
    created_utc: str
    mode: Literal["component", "circuit"]
    inputs: list[ArchiveEntry] = Field(default_factory=list)
    model_hashes: dict[str, str] = Field(default_factory=dict)
    test_hashes: dict[str, str] = Field(default_factory=dict)
    requirement_baseline_hash: str = ""
    tool_versions: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, str] = Field(default_factory=dict)
    provider: ProviderIdentity | None = None
    reader_backend: ReaderBackend = ReaderBackend.NATIVE
    resource_usage: dict[str, float] = Field(default_factory=dict)
    results_summary: dict[str, int] = Field(default_factory=dict)
    qualifications: list[str] = Field(default_factory=list)
    disclosures: list[DataDisclosure] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# findings and review


class Finding(Record):
    """A static or dynamic finding about a circuit or model."""

    code: str
    status: Status
    refdes: str | None = None
    nets: list[str] = Field(default_factory=list)
    message: str
    detail: dict[str, str] = Field(default_factory=dict)


class ReviewItem(Record):
    """A question the pipeline could not answer on its own."""

    id: str
    kind: Literal["ambiguity", "conflict", "missing_evidence"]
    question: str
    options: list[str] = Field(default_factory=list)
    recommended: str | None = None
    blocking: bool = False
    affected_requirement_ids: list[str] = Field(default_factory=list)
    resolution: str | None = None
