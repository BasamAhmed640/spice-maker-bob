"""Deterministic validation of extracted requirements.

This module is the gate between an extraction and the frozen baseline. It turns
a requirement list into machine-readable issues so that a structural defect — a
missing bound, an unknown unit, an absolute-maximum source used as an operating
limit, a duplicate id — is something the pipeline can refuse to build a baseline
from instead of something a human has to notice.

Three rules shape everything here:

* **Nothing is normalized silently.** An unknown unit is ``unit_unknown``; a
  value is never converted unless both units are in :data:`UNIT_VOCABULARY`.
* **A source that cannot be an operating limit is rejected, not reinterpreted.**
  Text from an absolute-maximum-ratings section stays rejected even when the
  numbers would look usable as limits.
* **A warning never reclassifies.** ``class_inference_mismatch`` points out that
  the evidence wording disagrees with the declared class; the declared class is
  left alone for a human (or the pipeline's review stage) to decide.

Issue codes are a stable wire format: they are asserted by tests and consumed by
reports, so a code is added deliberately and never renamed in place. The
severity of a code is part of the contract:

``error``
    the requirement must not enter the baseline as it stands.
``warning``
    the requirement may be usable, but something about it needs a decision.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from boardmodeler.documents.pdf import normalize_for_citation
from boardmodeler.domain.enums import RequirementClass, RequirementKind, RequirementOrigin
from boardmodeler.domain.expressions import ALL_OPS, Expr
from boardmodeler.domain.records import DocumentRecord, Limit, Requirement

__all__ = [
    "ABSOLUTE_MAXIMUM_MARKERS",
    "UNIT_VOCABULARY",
    "RequirementIssue",
    "UnitError",
    "UnknownUnitError",
    "ValidationReport",
    "classify_class",
    "expression_signals",
    "is_absolute_maximum",
    "normalize_unit",
    "scale_factor",
    "validate_requirement",
    "validate_requirements",
]


# --------------------------------------------------------------------------- #
# units


class UnitError(ValueError):
    """A unit cannot be used: it is unknown, or the two units are incompatible."""


class UnknownUnitError(UnitError):
    """The unit, after prefix folding, is not one of the canonical units."""


UNIT_VOCABULARY: dict[str, str] = {
    "V": "voltage",
    "A": "current",
    "s": "time",
    "ohm": "resistance",
    "C": "temperature",
    "W": "power",
    "Hz": "frequency",
    "F": "capacitance",
    "H": "inductance",
    "%": "ratio",
    "ratio": "ratio",
    "S": "conductance",
    "A/V": "transconductance",
    "V/V": "gain",
    "s/V": "inverse_voltage_slew",
    "V/s": "voltage_slew",
    "A/s": "current_slew",
    "C/W": "thermal_resistance",
    "K/W": "thermal_resistance",
    "cycles": "count",
}
"""Canonical unit symbol → physical dimension.

Deliberately small and closed: a unit outside this vocabulary is reported as
``unit_unknown`` rather than guessed at, because a wrongly interpreted unit is
worse than an uninterpreted one. Temperature appears only as Celsius —
:func:`scale_factor` converts prefixes, never offsets, so kelvin is excluded
rather than silently wrong by 273.15.
"""

_SI_PREFIXES: dict[str, float] = {
    "G": 1e9,
    "M": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
}

_MICRO_SIGNS = re.compile(r"[\u00b5\u03bc]")
_OHM_ALIAS = re.compile(r"^(?P<prefix>[GMkmunp]?)(?:ohms?|[\u03a9\u2126])$", re.IGNORECASE)
_DEGREE_C_ALIAS = re.compile(r"^deg(?:rees?)?\s*C$", re.IGNORECASE)
_PERCENT_ALIAS = re.compile(r"^per\s?cent$", re.IGNORECASE)


def normalize_unit(unit: str) -> str:
    """Canonical, prefix-free symbol for ``unit``.

    ``"mV"`` → ``"V"``, ``"kohm"`` → ``"ohm"``, ``"°C"`` → ``"C"``. The scale
    the prefix carried is available from :func:`scale_factor`. Raises
    :class:`UnknownUnitError` for anything outside the vocabulary — including an
    empty string, which is a missing unit, not a dimensionless one.
    """
    return _split_unit(unit)[1]


def scale_factor(from_unit: str, to_unit: str) -> float:
    """Multiply a ``from_unit`` value by this to express it in ``to_unit``.

    SI prefixes are the only conversion performed (``"mV"`` → ``"V"`` is
    ``0.001``). Raises :class:`UnknownUnitError` for a unit outside the
    vocabulary and :class:`UnitError` for two units of different dimensions.
    """
    from_scale, from_symbol = _split_unit(from_unit)
    to_scale, to_symbol = _split_unit(to_unit)
    if UNIT_VOCABULARY[from_symbol] != UNIT_VOCABULARY[to_symbol]:
        raise UnitError(
            f"cannot convert {from_unit!r} ({UNIT_VOCABULARY[from_symbol]}) to "
            f"{to_unit!r} ({UNIT_VOCABULARY[to_symbol]})"
        )
    return from_scale / to_scale


def _split_unit(unit: str) -> tuple[float, str]:
    """``(scale, canonical_symbol)``; prefixes apply to each quotient operand.

    Time per volt is not voltage per time. Recognizing a compound unit does not
    make it measurable by a probe, nor does it authorize taking its reciprocal.
    """
    text = _fold_unit(unit)
    if text in UNIT_VOCABULARY:
        return 1.0, text
    if text.count("/") == 1:
        numerator, denominator = text.split("/")
        # Kelvin here denotes a temperature difference; no absolute-temperature
        # conversion is introduced by accepting thermal resistance.
        numerator = _fold_unit(numerator)
        if numerator == "K":
            num_scale, num_symbol = 1.0, "K"
        elif numerator.endswith("K") and numerator[:-1] in _SI_PREFIXES:
            num_scale, num_symbol = _SI_PREFIXES[numerator[:-1]], "K"
        else:
            num_scale, num_symbol = _split_unit(numerator)
        den_scale, den_symbol = _split_unit(denominator)
        symbol = f"{num_symbol}/{den_symbol}"
        if symbol in UNIT_VOCABULARY:
            return num_scale / den_scale, symbol
    if len(text) > 1 and text[0] in _SI_PREFIXES and text[1:] in UNIT_VOCABULARY:
        return _SI_PREFIXES[text[0]], text[1:]
    raise UnknownUnitError(
        f"unit {unit!r} is not in the unit vocabulary: "
        f"{sorted(UNIT_VOCABULARY)} with SI prefixes {sorted(_SI_PREFIXES)}"
    )


def _fold_unit(unit: str) -> str:
    """Fold the spellings a datasheet may use onto the vocabulary symbols."""
    text = unit.strip()
    if not text:
        raise UnknownUnitError("the empty unit is not in the unit vocabulary")
    text = _MICRO_SIGNS.sub("u", text)
    text = text.replace("\u2103", "C").replace("\u00b0", "")
    ohm = _OHM_ALIAS.match(text)
    if ohm is not None:
        return f"{ohm.group('prefix')}ohm"
    if _DEGREE_C_ALIAS.match(text):
        return "C"
    if _PERCENT_ALIAS.match(text):
        return "%"
    return text


# --------------------------------------------------------------------------- #
# issues


@dataclass(frozen=True)
class RequirementIssue:
    """One machine-readable finding about a requirement."""

    severity: Literal["error", "warning"]
    code: str
    req_id: str | None
    message: str
    detail: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationReport:
    """Every issue in a requirement list plus the class breakdown.

    ``counts`` always carries every :class:`RequirementClass` member (zero
    included) so a report never has to distinguish "none" from "missing key".
    """

    issues: list[RequirementIssue]
    counts: dict[str, int]

    @property
    def errors(self) -> list[RequirementIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _error(code: str, req_id: str | None, message: str, **detail: str) -> RequirementIssue:
    return RequirementIssue(
        severity="error", code=code, req_id=req_id, message=message, detail=detail
    )


def _warning(code: str, req_id: str | None, message: str, **detail: str) -> RequirementIssue:
    return RequirementIssue(
        severity="warning", code=code, req_id=req_id, message=message, detail=detail
    )


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _describe_limits(limits: Limit | None) -> str:
    if limits is None:
        return "no limits"
    return f"min={_fmt(limits.min)}, typ={_fmt(limits.typ)}, max={_fmt(limits.max)} {limits.unit}"


# --------------------------------------------------------------------------- #
# wording


ABSOLUTE_MAXIMUM_MARKERS: tuple[str, ...] = (
    "absolute maximum",
    "absolute-maximum",
    "absolute max",
    "abs max",
    "maximum ratings",
    "stress rating",
    "stress beyond",
)
"""Phrases that mark a source as an absolute-maximum-ratings source.

Matched case-insensitively against whitespace/typography-normalized evidence
(:func:`boardmodeler.documents.pdf.normalize_for_citation`), so ``"ABS MAX"``,
``"Absolute\u2013Maximum"`` and ``"Stresses beyond"`` are all covered. A match
means the text describes destruction limits, not operating limits.
"""

_TYPICAL_WORDING = re.compile(r"\btyp(?:ical)?\b|\bnom(?:inal)?\b", re.IGNORECASE)
_BOUND_WORDING = re.compile(r"\bmin(?:imum)?\b|\bmax(?:imum)?\b", re.IGNORECASE)
_CONDITION_WORDING: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bT[_ ]?[AJC]\b"),  # TA, T_A, TJ, TC
    re.compile(r"\bV[_ ]?[A-Z]{1,4}\b\s*="),  # VIN =, V_CC =, VDDQ =
    re.compile(r"\bI[_ ]?[A-Z]{1,4}\b\s*="),  # IO =, I_OUT =, ISW =
    re.compile(r"\b\d+(?:\.\d+)?\s*°?\s*C\b"),  # 25 C, 85°C, 125 C
    re.compile(r"\b(?:full|no|light|heavy|nominal)\s+load\b", re.IGNORECASE),
    re.compile(r"\b(?:ambient|junction|case)\s+temperature\b", re.IGNORECASE),
)
"""Evidence wording that states an operating condition.

Only concrete patterns count: ``TA = 25 C``, ``VIN =``, ``IO =``, a numeric
temperature, a load point, an ambient/junction/case temperature. The bare word
"conditions" is deliberately not a pattern — a section *titled* "Recommended
Operating Conditions" states no condition by itself.
"""

_LIMIT_CLASSES = frozenset({RequirementClass.DOCUMENTED_LIMIT, RequirementClass.TYPICAL_VALUE})


def _normalized(text: str | None) -> str:
    return normalize_for_citation(text or "").casefold()


def _evidence_texts(requirement: Requirement) -> Iterator[tuple[str, str]]:
    """``(field, text)`` for every searched evidence field, in a fixed order."""
    for ref in requirement.evidence:
        yield "section", ref.section or ""
        yield "table", ref.table or ""
        yield "excerpt", ref.excerpt


def _joined_evidence(requirement: Requirement) -> str:
    """Normalized evidence text, case kept (``TA`` must not become ``ta``)."""
    return " ".join(
        normalize_for_citation(text) for _, text in _evidence_texts(requirement) if text.strip()
    )


def _inference_text(requirement: Requirement) -> str:
    """Case-folded evidence text used for wording inference.

    The statement is deliberately excluded: it is the extractor's own prose, so
    using it would let a declaration justify itself.
    """
    return _joined_evidence(requirement).casefold()


def _absolute_maximum_match(requirement: Requirement) -> tuple[str, str] | None:
    """``(marker, field)`` for the first absolute-maximum hit, or ``None``."""
    sources = (*_evidence_texts(requirement), ("statement", requirement.statement))
    for where, text in sources:
        haystack = _normalized(text)
        if not haystack:
            continue
        for marker in ABSOLUTE_MAXIMUM_MARKERS:
            if marker in haystack:
                return marker, where
    return None


def is_absolute_maximum(requirement: Requirement) -> bool:
    """Whether any of this requirement's sources is an absolute-maximum source.

    True when an evidence excerpt, section or table matches a marker in
    :data:`ABSOLUTE_MAXIMUM_MARKERS`, or when the statement itself carries
    absolute-maximum wording.
    """
    return _absolute_maximum_match(requirement) is not None


def classify_class(requirement: Requirement) -> RequirementClass:
    """Infer the class the requirement's own sources support.

    Precedence, from strongest signal to weakest:

    1. absolute-maximum wording → ``UNKNOWN``: such a source is not an operating
       limit in any class.
    2. explicit ``typical``/``typ.``/``nom`` wording → ``TYPICAL_VALUE``. This
       wins even when the requirement also carries min/max bounds, so a
       misclassified limit row is reported rather than excused.
    3. a min/max bound (structured on the limits, or stated in the evidence) →
       ``DOCUMENTED_LIMIT``.
    4. no signal at all → the declared class, because there is nothing to infer
       from and a silent source must not be "corrected".
    """
    if is_absolute_maximum(requirement):
        return RequirementClass.UNKNOWN
    text = _inference_text(requirement)
    if _TYPICAL_WORDING.search(text):
        return RequirementClass.TYPICAL_VALUE
    limits = requirement.limits
    bounded = limits is not None and (limits.min is not None or limits.max is not None)
    if bounded or _BOUND_WORDING.search(text):
        return RequirementClass.DOCUMENTED_LIMIT
    return requirement.req_class


# --------------------------------------------------------------------------- #
# expression traversal


@dataclass(frozen=True)
class _ExpressionFacts:
    ops: list[str]
    units: list[str]
    signals: list[str]


def _expression_facts(expr: Expr | None) -> _ExpressionFacts:
    """Pre-order walk of the closed AST collecting ops, units and signals."""
    ops: list[str] = []
    units: list[str] = []
    signals: list[str] = []
    if expr is None:
        return _ExpressionFacts(ops, units, signals)
    stack: list[BaseModel] = [expr]
    while stack:
        node = stack.pop()
        fields = type(node).model_fields
        for name, sink in (("op", ops), ("unit", units), ("signal", signals)):
            if name in fields:
                sink.append(str(getattr(node, name)))
        children: list[BaseModel] = []
        for name in fields:
            value = getattr(node, name)
            if isinstance(value, BaseModel):
                children.append(value)
            elif isinstance(value, list):
                children.extend(item for item in value if isinstance(item, BaseModel))
        stack.extend(reversed(children))
    return _ExpressionFacts(ops, units, signals)


def expression_signals(expr: Expr | None) -> list[str]:
    """Unique signal names named by ``expr``, in first-appearance order.

    Connectivity ops name nets rather than signals and contribute nothing; a
    purely connectivity expression therefore returns ``[]``.
    """
    return list(dict.fromkeys(_expression_facts(expr).signals))


# --------------------------------------------------------------------------- #
# per-requirement checks


def validate_requirement(
    requirement: Requirement, *, document: DocumentRecord | None = None
) -> list[RequirementIssue]:
    """Every issue this one requirement has, in a stable rule order.

    ``document`` is the record to check document-level facts against (normally
    the requirement's primary source): with it, a citation whose page is outside
    the document is an error. Passing ``None`` skips those checks — it never
    weakens the checks that need no document.
    """
    issues: list[RequirementIssue] = []
    issues.extend(_check_limit_order(requirement))
    issues.extend(_check_limit_presence(requirement))
    issues.extend(_check_units(requirement))
    issues.extend(_check_absolute_maximum(requirement))
    issues.extend(_check_class_inference(requirement))
    issues.extend(_check_citation(requirement, document))
    issues.extend(_check_conditions(requirement))
    issues.extend(_check_expression(requirement))
    return issues


def _check_limit_order(requirement: Requirement) -> list[RequirementIssue]:
    limits = requirement.limits
    if limits is None:
        return []
    present = [value for value in (limits.min, limits.typ, limits.max) if value is not None]
    if present == sorted(present):
        return []
    # ``Limit`` rejects this at construction, so reaching it means a record was
    # assembled without validation (``model_copy`` bypasses it).
    return [
        _error(
            "limit_triple_order",
            requirement.req_id,
            f"limit values are out of order (min/typ/max = "
            f"{_fmt(limits.min)}/{_fmt(limits.typ)}/{_fmt(limits.max)} {limits.unit})",
            min=_fmt(limits.min),
            typ=_fmt(limits.typ),
            max=_fmt(limits.max),
            unit=limits.unit,
        )
    ]


_NUMERIC_KINDS = frozenset({RequirementKind.ELECTRICAL, RequirementKind.TEMPORAL})
"""Kinds whose documented limits are inherently numeric.

A connectivity or functional requirement is routinely stated without a number
("the pad must be soldered down"), so requiring a bound for those kinds would
flag correct device data. The check still catches the case it exists for: an
electrical or timing limit that lost its numbers.
"""


def _check_limit_presence(requirement: Requirement) -> list[RequirementIssue]:
    """A documented limit needs a number or a machine-checkable form.

    A datasheet states some limits without numbers ("the exposed pad must be
    soldered down", "switching must not resume before SS/TR has discharged"), and
    those are still limits — but they are only usable once they have an
    ``expression``. A limit with neither a numeric bound nor an expression is a
    hole in the requirement set, and that is what this check reports.
    """
    limits = requirement.limits
    klass = requirement.req_class
    if klass is RequirementClass.DOCUMENTED_LIMIT:
        has_bound = limits is not None and (limits.min is not None or limits.max is not None)
        if not has_bound and requirement.expression is None:
            return [
                _error(
                    "limit_missing_value",
                    requirement.req_id,
                    "a DOCUMENTED_LIMIT must carry a min/max bound or a machine-checkable "
                    "expression",
                    req_class=klass.value,
                    limits=_describe_limits(limits),
                    kind=requirement.kind.value,
                )
            ]
    elif klass is RequirementClass.TYPICAL_VALUE and (limits is None or limits.typ is None):
        return [
            _error(
                "limit_missing_value",
                requirement.req_id,
                "a TYPICAL_VALUE must carry a typ value",
                req_class=klass.value,
                limits=_describe_limits(limits),
            )
        ]
    return []


def _check_units(requirement: Requirement) -> list[RequirementIssue]:
    pairs: list[tuple[str, str]] = []
    if requirement.limits is not None:
        pairs.append(("limits", requirement.limits.unit))
    pairs.extend(("expression", unit) for unit in _expression_facts(requirement.expression).units)
    issues: list[RequirementIssue] = []
    seen: set[tuple[str, str]] = set()
    for field_name, unit in pairs:
        if (field_name, unit) in seen:
            continue
        seen.add((field_name, unit))
        try:
            normalize_unit(unit)
        except UnknownUnitError:
            issues.append(
                _error(
                    "unit_unknown",
                    requirement.req_id,
                    f"{field_name} unit {unit!r} is not in the unit vocabulary",
                    field=field_name,
                    unit=unit,
                )
            )
    return issues


def _check_absolute_maximum(requirement: Requirement) -> list[RequirementIssue]:
    match = _absolute_maximum_match(requirement)
    if match is None or requirement.req_class not in _LIMIT_CLASSES:
        return []
    marker, where = match
    return [
        _error(
            "absolute_maximum_rejected",
            requirement.req_id,
            f"{where} matches the absolute-maximum marker {marker!r}, so this source is not usable "
            f"as a {requirement.req_class.value}",
            marker=marker,
            matched_in=where,
            req_class=requirement.req_class.value,
        )
    ]


def _check_class_inference(requirement: Requirement) -> list[RequirementIssue]:
    inferred = classify_class(requirement)
    declared = requirement.req_class
    if declared not in _LIMIT_CLASSES or inferred not in _LIMIT_CLASSES or inferred is declared:
        return []
    return [
        _warning(
            "class_inference_mismatch",
            requirement.req_id,
            f"the evidence wording indicates {inferred.value} but the requirement is classed "
            f"{declared.value}",
            declared_class=declared.value,
            inferred_class=inferred.value,
        )
    ]


def _check_citation(
    requirement: Requirement, document: DocumentRecord | None
) -> list[RequirementIssue]:
    if requirement.origin is not RequirementOrigin.DOCUMENT:
        return []
    if not requirement.evidence:
        return [
            _error(
                "citation_missing",
                requirement.req_id,
                "a DOCUMENT requirement must carry at least one evidence reference",
            )
        ]
    if document is None or document.page_count <= 0:
        return []
    issues: list[RequirementIssue] = []
    for ref in requirement.evidence:
        if ref.doc_id != document.doc_id or ref.page is None:
            continue
        if ref.page.pdf_page >= document.page_count:
            issues.append(
                _error(
                    "citation_page_out_of_range",
                    requirement.req_id,
                    f"the cited page {ref.page.pdf_page} is outside {document.doc_id} "
                    f"({document.page_count} pages)",
                    doc_id=document.doc_id,
                    pdf_page=str(ref.page.pdf_page),
                    page_count=str(document.page_count),
                )
            )
    return issues


def _check_conditions(requirement: Requirement) -> list[RequirementIssue]:
    if requirement.conditions:
        return []
    text = _joined_evidence(requirement)
    for pattern in _CONDITION_WORDING:
        match = pattern.search(text)
        if match is not None:
            return [
                _warning(
                    "conditions_missing",
                    requirement.req_id,
                    f"the evidence states a condition ({match.group(0)!r}) but the requirement "
                    f"lists no conditions",
                    matched=match.group(0),
                )
            ]
    return []


def _check_expression(requirement: Requirement) -> list[RequirementIssue]:
    facts = _expression_facts(requirement.expression)
    issues: list[RequirementIssue] = []
    for op in dict.fromkeys(facts.ops):
        if op not in ALL_OPS:
            issues.append(
                _error(
                    "expression_op_unknown",
                    requirement.req_id,
                    f"expression op {op!r} is not one of the closed op set",
                    op=op,
                )
            )
    if facts.signals and not requirement.signal_refs:
        signals = ", ".join(dict.fromkeys(facts.signals))
        issues.append(
            _warning(
                "signal_refs_missing",
                requirement.req_id,
                f"the expression names the signal(s) {signals} but signal_refs is empty",
                signals=signals,
            )
        )
    return issues


# --------------------------------------------------------------------------- #
# list validation


def validate_requirements(
    requirements: Sequence[Requirement],
    *,
    documents: Mapping[str, DocumentRecord] | None = None,
) -> ValidationReport:
    """Validate a whole list: per-requirement issues plus duplicate ids.

    Issues appear in requirement order, with the ``duplicate_req_id`` errors
    appended last (they belong to the list, not to one position in it). With
    ``documents``, each requirement is checked against the first document it
    cites that is present in the mapping.
    """
    ordered = list(requirements)
    issues: list[RequirementIssue] = []
    counts = {klass.value: 0 for klass in RequirementClass}
    occurrences: dict[str, int] = {}
    for requirement in ordered:
        issues.extend(
            validate_requirement(requirement, document=_document_for(requirement, documents))
        )
        counts[requirement.req_class.value] += 1
        occurrences[requirement.req_id] = occurrences.get(requirement.req_id, 0) + 1
    for req_id, count in occurrences.items():
        if count > 1:
            issues.append(
                _error(
                    "duplicate_req_id",
                    req_id,
                    f"req_id {req_id!r} is used by {count} requirements, but ids must be unique",
                    count=str(count),
                )
            )
    return ValidationReport(issues=issues, counts=counts)


def _document_for(
    requirement: Requirement, documents: Mapping[str, DocumentRecord] | None
) -> DocumentRecord | None:
    if not documents:
        return None
    for ref in requirement.evidence:
        document = documents.get(ref.doc_id)
        if document is not None:
            return document
    return None
