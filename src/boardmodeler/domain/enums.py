"""Enumerations that appear in serialized records.

Every enum member name is part of the wire format: records are dumped with
``model_dump_json`` and compared by hash in baselines, so member renames are
schema changes and must bump :data:`boardmodeler.domain.records.SCHEMA_VERSION`.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Criticality",
    "EvidenceExtraction",
    "EvidenceLevel",
    "ModelKind",
    "ProviderKind",
    "ReaderBackend",
    "RequirementClass",
    "RequirementKind",
    "RequirementOrigin",
    "Status",
]


class Status(StrEnum):
    """Outcome of a check. ``PASS`` always requires an observed measurement."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceLevel(StrEnum):
    """How strongly a model artifact is backed by observed evidence.

    Ordered from weakest to strongest; a model never claims a level it did not
    achieve (no bench data exists in this repository, so ``BENCH_CORRELATED``
    is not reachable here).
    """

    SYNTHETIC_ANALYTICAL = "SYNTHETIC_ANALYTICAL"
    DATASHEET_CORRELATED = "DATASHEET_CORRELATED"
    VENDOR_MODEL_COMPARED = "VENDOR_MODEL_COMPARED"
    BENCH_CORRELATED = "BENCH_CORRELATED"
    ENGINEER_REVIEWED = "ENGINEER_REVIEWED"


class RequirementClass(StrEnum):
    """Class of a requirement statement."""

    DOCUMENTED_LIMIT = "DOCUMENTED_LIMIT"
    ABSOLUTE_MAXIMUM = "ABSOLUTE_MAXIMUM"
    TYPICAL_VALUE = "TYPICAL_VALUE"
    DERIVED_VALUE = "DERIVED_VALUE"
    USER_REQUIREMENT = "USER_REQUIREMENT"
    ASSUMPTION = "ASSUMPTION"
    UNKNOWN = "UNKNOWN"


class RequirementKind(StrEnum):
    """Subject area of a requirement."""

    ELECTRICAL = "ELECTRICAL"
    FUNCTIONAL = "FUNCTIONAL"
    TEMPORAL = "TEMPORAL"
    CONNECTIVITY = "CONNECTIVITY"
    SYSTEM = "SYSTEM"


class Criticality(StrEnum):
    """Effect of violating the requirement on bring-up."""

    CRITICAL = "CRITICAL"
    IMPORTANT = "IMPORTANT"
    INFORMATIONAL = "INFORMATIONAL"


class RequirementOrigin(StrEnum):
    """Where a requirement came from. ``TEST_FIXTURE`` marks synthetic data."""

    DOCUMENT = "DOCUMENT"
    TEST_FIXTURE = "TEST_FIXTURE"
    USER = "USER"


class ModelKind(StrEnum):
    """How a simulation model was obtained."""

    VENDOR_PIN_COMPATIBLE = "VENDOR_PIN_COMPATIBLE"
    REDUCED_BEHAVIORAL = "REDUCED_BEHAVIORAL"


class EvidenceExtraction(StrEnum):
    """How the text of an evidence excerpt was obtained."""

    EMBEDDED_TEXT = "embedded_text"
    OCR = "ocr"
    FIGURE_READ = "figure_read"
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    USER_SUPPLIED = "user_supplied"


class ReaderBackend(StrEnum):
    """Which ``.raw`` reader produced waveform data for a run."""

    NATIVE = "native"
    SPICELIB = "spicelib"


class ProviderKind(StrEnum):
    """Extraction provider family. Selected explicitly; never silently swapped."""

    FIXTURE = "FIXTURE"
    HTTP_INFERENCE = "HTTP_INFERENCE"
    BOB_DIRECT = "BOB_DIRECT"
    BOB_SHELL = "BOB_SHELL"
