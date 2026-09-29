"""Source-consistency review: citation verification, conflicts, review items.

This module stands between an extraction and the baseline. It answers three
questions and never answers a fourth:

* **Is the citation real?** Every ``EvidenceRef`` of a ``DOCUMENT`` requirement
  must name a document the project has, a page, and a non-empty excerpt, and the
  excerpt must be *found on that page* of that document. A citation is never
  verified because the document exists, because the page exists, or because the
  text is close enough — that is the product's central claim, so
  :func:`verify_citations` returns ``False`` whenever it cannot prove otherwise.
* **Do sources disagree?** :func:`find_conflicts` reports pairs whose allowed
  intervals are disjoint for the same quantity, and pairs that quote the same
  statement from different documents with different values.
* **What needs a human?** :func:`review` turns those findings into
  :class:`~boardmodeler.domain.records.ReviewItem` questions with concrete
  options; :func:`apply_review` writes the honest defaults back onto the
  requirements (``citation_verified=False``, ``status="conflict"``) without
  touching the expression, because the engine already turns an unverified
  citation into UNKNOWN by itself.

``origin != DOCUMENT`` requirements (``TEST_FIXTURE``/``USER``) are not citation
verified at all: they appear in neither ``verified`` nor ``unverified``, and the
caller treats them as user/fixture data rather than as document-derived device
data.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from boardmodeler.documents.pdf import (
    PdfDocument,
    excerpt_on_page,
    normalize_for_citation,
    read_pdf,
)
from boardmodeler.domain.enums import Criticality, RequirementOrigin
from boardmodeler.domain.expressions import Expr
from boardmodeler.domain.records import (
    DocumentRecord,
    EvidenceRef,
    Limit,
    Requirement,
    ReviewItem,
)
from boardmodeler.requirements.model import (
    RequirementIssue,
    UnitError,
    expression_signals,
    normalize_unit,
    scale_factor,
    validate_requirements,
)

__all__ = [
    "ReviewOutcome",
    "apply_review",
    "find_conflicts",
    "review",
    "verify_citations",
]

EXCERPT_MAX_CHARS = 400
"""How much of an excerpt is compared; matches ``EvidenceRef.excerpt``'s cap."""

_HYPHEN_BREAK = re.compile(r"-\s")


# --------------------------------------------------------------------------- #
# outcome


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of reviewing a requirement list against its documents.

    ``verified``/``unverified`` hold ``DOCUMENT`` requirement ids only.
    ``coverage`` is ``verified / (verified + unverified)``, so requirements that
    are not citation-verified by design do not dilute the ratio; it is ``0.0``
    when there is nothing to verify. ``items`` are the questions a human has to
    answer, and ``issues`` are the machine-readable findings behind them.
    """

    verified: list[str]
    unverified: list[str]
    conflicts: list[str]
    items: list[ReviewItem]
    issues: list[RequirementIssue]

    @property
    def coverage(self) -> float:
        total = len(self.verified) + len(self.unverified)
        if total == 0:
            return 0.0
        return len(self.verified) / total


# --------------------------------------------------------------------------- #
# citation verification


@dataclass(frozen=True)
class _CitationCheck:
    """Why one requirement's citation did (not) verify."""

    verified: bool
    reason: str = ""
    doc_id: str | None = None
    pdf_page: int | None = None


def verify_citations(
    requirements: Sequence[Requirement],
    documents: Mapping[str, DocumentRecord],
    *,
    excerpt_lookup: Callable[[str, int], str | None] | None = None,
) -> dict[str, bool]:
    """Whether each ``DOCUMENT`` requirement's citations were found.

    ``documents`` is keyed by ``doc_id``. Page text comes from
    ``excerpt_lookup(doc_id, pdf_page)`` when one is supplied, otherwise the PDF
    at ``DocumentRecord.path`` is parsed with
    :func:`boardmodeler.documents.pdf.read_pdf` (once per document per call,
    reading only the pages that are cited) and matched with
    :func:`boardmodeler.documents.pdf.excerpt_on_page`. ``path`` is used exactly
    as recorded, so a project-relative path resolves against the process working
    directory — pass absolute paths (``DocumentStore.original_path``) when that
    is not the project root.

    A requirement is ``True`` only when **every** evidence reference has a
    known document, a recorded page, a non-empty excerpt, and an excerpt that is
    present on that page's extracted text. Anything else — a missing document, a
    document with ``text_extraction="none"``, a page that does not exist, an
    unreadable file, an invented excerpt — is ``False``.

    Requirements whose ``origin`` is not ``DOCUMENT`` (``TEST_FIXTURE``/``USER``)
    are not citation-verified and appear in neither the returned mapping's
    ``True`` nor its ``False`` entries.

    Results are deterministic: requirements are visited in order, so the
    mapping's insertion order and contents are stable for the same inputs.
    """
    checks, _ = _verify_citations(list(requirements), documents, excerpt_lookup)
    return {req_id: check.verified for req_id, check in checks.items()}


def _verify_citations(
    requirements: Sequence[Requirement],
    documents: Mapping[str, DocumentRecord],
    excerpt_lookup: Callable[[str, int], str | None] | None,
) -> tuple[dict[str, _CitationCheck], list[RequirementIssue]]:
    verifier = _Verifier(documents, excerpt_lookup, _pages_needed(requirements, excerpt_lookup))
    checks: dict[str, _CitationCheck] = {}
    for requirement in requirements:
        if requirement.origin is not RequirementOrigin.DOCUMENT:
            continue
        checks[requirement.req_id] = verifier.check(requirement)
    return checks, verifier.issues


def _pages_needed(
    requirements: Sequence[Requirement],
    excerpt_lookup: Callable[[str, int], str | None] | None,
) -> dict[str, int]:
    """Per document, ``max(cited page) + 1``, so a datasheet is not read whole."""
    if excerpt_lookup is not None:
        return {}
    needed: dict[str, int] = {}
    for requirement in requirements:
        if requirement.origin is not RequirementOrigin.DOCUMENT:
            continue
        for ref in requirement.evidence:
            if ref.page is not None:
                needed[ref.doc_id] = max(needed.get(ref.doc_id, 0), ref.page.pdf_page + 1)
    return needed


class _Verifier:
    """One verification pass: cached PDFs, collected issues, no shared state."""

    def __init__(
        self,
        documents: Mapping[str, DocumentRecord],
        excerpt_lookup: Callable[[str, int], str | None] | None,
        pages_needed: Mapping[str, int],
    ) -> None:
        self._documents = documents
        self._lookup = excerpt_lookup
        self._pages_needed = pages_needed
        self._cache: dict[str, PdfDocument] = {}
        self.issues: list[RequirementIssue] = []

    def check(self, requirement: Requirement) -> _CitationCheck:
        if not requirement.evidence:
            # ``citation_missing`` (validation, error) already reports this.
            return _CitationCheck(False, "the requirement carries no evidence reference to verify")
        for ref in requirement.evidence:
            failure = self._check_ref(requirement.req_id, ref)
            if failure is not None:
                return failure
        return _CitationCheck(True)

    # ------------------------------------------------------------------ internals

    def _check_ref(self, req_id: str, ref: EvidenceRef) -> _CitationCheck | None:
        """``None`` when this reference verified, else the failing check."""
        doc_id = ref.doc_id.strip()
        if not doc_id:
            self._warn(
                "citation_doc_unknown",
                req_id,
                "an evidence reference names no document, so nothing can be checked",
                doc_id="",
            )
            return _CitationCheck(False, "an evidence reference names no document")
        document = self._documents.get(doc_id)
        if document is None:
            self._warn(
                "citation_doc_unknown",
                req_id,
                f"the evidence cites {doc_id!r}, which is not one of the project's documents",
                doc_id=doc_id,
            )
            return _CitationCheck(
                False, f"the cited document {doc_id!r} is not among the project's documents", doc_id
            )
        if document.text_extraction == "none":
            self._warn(
                "citation_unverifiable_no_text",
                req_id,
                f"{doc_id} reports text_extraction='none', so its citations cannot be checked",
                doc_id=doc_id,
            )
            return _CitationCheck(
                False,
                f"{doc_id} has no extracted text (text_extraction='none'), so its citation "
                f"cannot be checked",
                doc_id,
            )
        if ref.page is None:
            self._warn(
                "citation_page_missing",
                req_id,
                f"the evidence citing {doc_id} records no page",
                doc_id=doc_id,
            )
            return _CitationCheck(False, f"the citation to {doc_id} records no page", doc_id)
        page = ref.page.pdf_page
        if document.page_count > 0 and page >= document.page_count:
            # A citation to a page that does not exist is a data defect; the
            # validation layer reports it as ``citation_page_out_of_range``.
            return _CitationCheck(
                False,
                f"page {page} is outside {doc_id} ({document.page_count} pages)",
                doc_id,
                page,
            )
        if not normalize_for_citation(ref.excerpt):
            self._warn(
                "citation_excerpt_empty",
                req_id,
                f"the evidence citing {doc_id} page {page} carries no excerpt text",
                doc_id=doc_id,
                pdf_page=str(page),
            )
            return _CitationCheck(
                False, f"the citation to {doc_id} page {page} carries no excerpt text", doc_id, page
            )
        if self._lookup is not None:
            text = self._lookup(doc_id, page)
            found = text is not None and _excerpt_in(text, ref.excerpt)
        else:
            pdf, reason = self._load(document)
            if pdf is None:
                self._warn(
                    "citation_source_unreadable",
                    req_id,
                    f"{doc_id} could not be read for verification: {reason}",
                    doc_id=doc_id,
                )
                return _CitationCheck(
                    False, f"{doc_id} could not be read for verification: {reason}", doc_id, page
                )
            try:
                found = excerpt_on_page(pdf, ref.excerpt, page)
            except IndexError:
                self._warn(
                    "citation_page_missing",
                    req_id,
                    f"page {page} does not exist in {doc_id}",
                    doc_id=doc_id,
                    pdf_page=str(page),
                )
                return _CitationCheck(
                    False, f"page {page} does not exist in {doc_id}", doc_id, page
                )
        if not found:
            self._warn(
                "citation_excerpt_not_found",
                req_id,
                f"the excerpt {_label(ref.excerpt)} was not found on page {page} of {doc_id}",
                doc_id=doc_id,
                pdf_page=str(page),
            )
            return _CitationCheck(
                False,
                f"the excerpt {_label(ref.excerpt)} was not found on page {page} of {doc_id}",
                doc_id,
                page,
            )
        return None

    def _load(self, document: DocumentRecord) -> tuple[PdfDocument | None, str]:
        """Read and cache the document; ``(None, reason)`` when unreadable."""
        cached = self._cache.get(document.doc_id)
        if cached is not None:
            return cached, ""
        if not document.path:
            return None, f"{document.doc_id} records no file path"
        try:
            path = Path(document.path)
            if not path.is_file():
                return None, f"{document.doc_id} records {document.path!r}, which is not a file"
            pdf = read_pdf(path, max_pages=self._pages_needed.get(document.doc_id))
        except Exception as exc:  # a reader failure is reported, never raised away
            return None, f"{document.doc_id} could not be read ({type(exc).__name__}: {exc})"
        self._cache[document.doc_id] = pdf
        return pdf, ""

    def _warn(self, code: str, req_id: str | None, message: str, **detail: str) -> None:
        self.issues.append(
            RequirementIssue(
                severity="warning", code=code, req_id=req_id, message=message, detail=detail
            )
        )


#: Separates independent readings of one page inside a lookup result. Two PDF readers can
#: space the same printed text differently ("VC M" against "V C M"), so a lookup may
#: return both readings and an excerpt verifies when any single reading contains it.
READING_BREAK = chr(12)


def _excerpt_in(page_text: str, excerpt: str) -> bool:
    """Whether ``excerpt`` appears in ``page_text`` after citation normalization.

    Same contract as :func:`boardmodeler.documents.pdf.excerpt_on_page` — only
    the first :data:`EXCERPT_MAX_CHARS` characters are compared, typography and
    whitespace are folded, and both sides are additionally tried with printed
    line-break hyphenation joined, because PDF extraction keeps the line breaks.
    An empty needle or an empty page never matches. ``page_text`` may hold several
    readings of the page separated by :data:`READING_BREAK`; the excerpt is compared with
    each reading on its own, never across the break.
    """
    needle = normalize_for_citation(excerpt[:EXCERPT_MAX_CHARS])
    if not needle:
        return False
    variants = {needle, _HYPHEN_BREAK.sub("", needle)}
    for reading in page_text.split(READING_BREAK):
        haystack = normalize_for_citation(reading)
        if not haystack:
            continue
        texts = {haystack, _HYPHEN_BREAK.sub("", haystack)}
        if any(variant in text for variant in variants for text in texts):
            return True
    return False


def _label(excerpt: str) -> str:
    text = " ".join(excerpt.split())
    if len(text) > 60:
        return f"{text[:57]!r}..."
    return repr(text)


# --------------------------------------------------------------------------- #
# conflicts


def find_conflicts(requirements: Sequence[Requirement]) -> list[tuple[str, str, str]]:
    """``(req_a, req_b, reason)`` for every pair of requirements that disagree.

    Two requirements conflict when they apply to the same ``applies_to``, the
    same quantity (the expression's primary signal, falling back to the first
    ``signal_refs`` entry) and the same ``configuration``, and the intervals they
    allow are disjoint — for example one allows at most 3.366 V while the other
    requires at least 3.5 V. Bounds come from the ``limits`` triple (min/max are
    bounds; ``typ`` is a nominal value) and, when no bound is recorded there, from
    a simple comparison expression. Intervals that merely touch are not a
    conflict, and a conditional or disjunctive expression is never interpreted as
    an interval.

    A pair whose identical statement is quoted from *different* documents with
    different stated values also conflicts: that is how a datasheet and an errata
    sheet disagree without either one looking wrong on its own.

    ``superseded`` requirements are not compared. A conflict is reported, never
    resolved: the reason names both requirements and the values, and
    :func:`apply_review` only marks the pair as ``status="conflict"``.
    """
    ordered = list(requirements)
    found: list[tuple[str, str, str]] = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            reason = _conflict_reason(first, second)
            if reason is not None:
                found.append((first.req_id, second.req_id, reason))
    return found


def _conflict_reason(first: Requirement, second: Requirement) -> str | None:
    if first.status == "superseded" or second.status == "superseded":
        return None
    if first.applies_to != second.applies_to:
        return None
    same_quantity = _quantity_key(first) == _quantity_key(second)
    same_configuration = _configuration_key(first) == _configuration_key(second)
    if same_quantity and same_configuration:
        reason = _interval_conflict(first, second)
        if reason is not None:
            return reason
    return _statement_conflict(first, second)


@dataclass(frozen=True)
class _Constraint:
    """The interval a requirement allows, in one canonical unit."""

    low: float | None
    high: float | None
    unit: str


def _interval_conflict(first: Requirement, second: Requirement) -> str | None:
    left = _constraint(first)
    right = _constraint(second)
    if left is None or right is None or left.unit != right.unit:
        return None
    note = _condition_note(first, second)
    if left.high is not None and right.low is not None and left.high < right.low:
        return (
            f"{first.req_id} allows at most {_fmt(left.high)} {left.unit}, which is below "
            f"{second.req_id}'s minimum of {_fmt(right.low)} {right.unit}{note}"
        )
    if right.high is not None and left.low is not None and right.high < left.low:
        return (
            f"{second.req_id} allows at most {_fmt(right.high)} {right.unit}, which is below "
            f"{first.req_id}'s minimum of {_fmt(left.low)} {left.unit}{note}"
        )
    return None


def _constraint(requirement: Requirement) -> _Constraint | None:
    bounds = _limits_constraint(requirement.limits)
    if bounds is None:
        bounds = _expression_bounds(requirement.expression)
    return bounds


def _limits_constraint(limits: Limit | None) -> _Constraint | None:
    if limits is None:
        return None
    if limits.min is None and limits.max is None:
        return None
    try:
        unit = normalize_unit(limits.unit)
        factor = scale_factor(limits.unit, unit)
    except UnitError:
        return None
    return _Constraint(
        None if limits.min is None else limits.min * factor,
        None if limits.max is None else limits.max * factor,
        unit,
    )


def _expression_bounds(expr: Expr | None) -> _Constraint | None:
    """Bounds stated directly by simple comparison ops; ``None`` when unclear."""
    if expr is None:
        return None
    op = expr.op
    if op in {"lt", "le"}:
        return _bounds_from(None, expr.value, expr.unit)
    if op in {"gt", "ge"}:
        return _bounds_from(expr.value, None, expr.unit)
    if op == "between":
        return _bounds_from(expr.low, expr.high, expr.unit)
    if op == "all_of":
        primary = _first_signal(expr)
        merged: _Constraint | None = None
        for item in expr.items:
            if _first_signal(item) != primary:
                continue  # a bound on another signal is a different interval
            part = _expression_bounds(item)
            if part is None:
                continue
            merged = part if merged is None else _intersect(merged, part)
        return merged
    return None


def _bounds_from(low: float | None, high: float | None, unit: str) -> _Constraint | None:
    try:
        canonical = normalize_unit(unit)
        factor = scale_factor(unit, canonical)
    except UnitError:
        return None
    return _Constraint(
        None if low is None else low * factor,
        None if high is None else high * factor,
        canonical,
    )


def _intersect(first: _Constraint, second: _Constraint) -> _Constraint:
    if first.unit != second.unit:
        return first
    return _Constraint(
        _tightest_low(first.low, second.low),
        _tightest_high(first.high, second.high),
        first.unit,
    )


def _tightest_low(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _tightest_high(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def _statement_conflict(first: Requirement, second: Requirement) -> str | None:
    statement = normalize_for_citation(first.statement).casefold()
    if not statement or statement != normalize_for_citation(second.statement).casefold():
        return None
    first_docs = {ref.doc_id for ref in first.evidence if ref.doc_id}
    second_docs = {ref.doc_id for ref in second.evidence if ref.doc_id}
    if not first_docs or not second_docs or not first_docs.isdisjoint(second_docs):
        return None
    first_values = _value_signature(first)
    second_values = _value_signature(second)
    if first_values is None or second_values is None or first_values == second_values:
        return None
    return (
        f"the same statement is quoted from {min(first_docs)} and {min(second_docs)} with "
        f"different values ({first_values} vs {second_values})"
    )


def _value_signature(requirement: Requirement) -> str | None:
    """Every stated limit as a canonical string, or ``None`` when uncomparable."""
    limits = requirement.limits
    if limits is None:
        return None
    try:
        unit = normalize_unit(limits.unit)
        factor = scale_factor(limits.unit, unit)
    except UnitError:
        return None
    parts = [
        f"{name} {_fmt(value * factor)} {unit}"
        for name, value in (("min", limits.min), ("typ", limits.typ), ("max", limits.max))
        if value is not None
    ]
    return ", ".join(parts) if parts else None


def _quantity_key(requirement: Requirement) -> str | None:
    signals = expression_signals(requirement.expression)
    if signals:
        return signals[0]
    if requirement.signal_refs:
        return requirement.signal_refs[0]
    return None


def _configuration_key(requirement: Requirement) -> str:
    return normalize_for_citation(requirement.configuration or "").casefold()


def _first_signal(expr: Expr | None) -> str | None:
    signals = expression_signals(expr)
    return signals[0] if signals else None


def _condition_note(first: Requirement, second: Requirement) -> str:
    notes = [
        f"{requirement.req_id}: {'; '.join(condition.text for condition in requirement.conditions)}"
        for requirement in (first, second)
        if requirement.conditions
    ]
    return f" [conditions — {' | '.join(notes)}]" if notes else ""


def _fmt(value: float) -> str:
    return f"{value:g}"


# --------------------------------------------------------------------------- #
# review


_CITATION_OPTIONS = ["accept as UNKNOWN", "supply the correct page", "reject the requirement"]
_CONFLICT_OPTIONS = (
    "keep {first} and mark {second} superseded",
    "keep {second} and mark {first} superseded",
    "keep both (their tests stay UNKNOWN)",
    "reject both requirements",
)
_KEEP_BOTH = "keep both (their tests stay UNKNOWN)"
_AMBIGUITY_GUIDANCE: dict[str, tuple[tuple[str, ...], str]] = {
    "class_inference_mismatch": (
        ("reclassify the requirement", "keep the declared class", "reject the requirement"),
        "reclassify the requirement",
    ),
    "conditions_missing": (
        (
            "accept without conditions",
            "add the conditions from the evidence",
            "reject the requirement",
        ),
        "add the conditions from the evidence",
    ),
    "signal_refs_missing": (
        ("accept without signal refs", "add the signal names", "reject the requirement"),
        "add the signal names",
    ),
}
_DEFAULT_AMBIGUITY = (
    ("accept the requirement as stated", "edit the requirement", "reject the requirement"),
    "accept the requirement as stated",
)


def review(
    requirements: Sequence[Requirement],
    documents: Mapping[str, DocumentRecord],
    *,
    excerpt_lookup: Callable[[str, int], str | None] | None = None,
) -> ReviewOutcome:
    """Review a requirement list against its documents.

    One :class:`ReviewItem` is produced per unverified citation
    (``missing_evidence``, blocking when the requirement is ``CRITICAL``), per
    conflict (``conflict``, always blocking), and per validation warning that
    falls on a ``CRITICAL`` requirement (``ambiguity``, never blocking — a
    warning asks for a decision, it does not stop the run). Item ids are derived
    from the requirement ids, so the same inputs always produce the same
    ``ReviewOutcome``, in the same order.
    """
    ordered = list(requirements)
    checks, verification_issues = _verify_citations(ordered, documents, excerpt_lookup)
    validation = validate_requirements(ordered, documents=documents)
    conflicts = find_conflicts(ordered)

    by_id: dict[str, Requirement] = {}
    for requirement in ordered:
        by_id.setdefault(requirement.req_id, requirement)

    verified: list[str] = []
    unverified: list[str] = []
    for requirement in ordered:
        if requirement.origin is not RequirementOrigin.DOCUMENT:
            continue
        check = checks.get(requirement.req_id)
        if check is None:
            continue
        target = verified if check.verified else unverified
        if requirement.req_id not in target:
            target.append(requirement.req_id)

    conflict_ids: list[str] = []
    conflicting = {req_id for first, second, _ in conflicts for req_id in (first, second)}
    for requirement in ordered:
        if requirement.req_id in conflicting and requirement.req_id not in conflict_ids:
            conflict_ids.append(requirement.req_id)

    items: list[ReviewItem] = []
    seen: set[str] = set()

    for req_id in unverified:
        check = checks[req_id]
        requirement = by_id.get(req_id)
        _append(
            items,
            seen,
            ReviewItem(
                id=f"RV_citation_{req_id}",
                kind="missing_evidence",
                question=(
                    f"Citation for {req_id} could not be verified: {check.reason}. "
                    f"How should this requirement be treated?"
                ),
                options=list(_CITATION_OPTIONS),
                recommended="accept as UNKNOWN",
                blocking=requirement is not None
                and requirement.criticality is Criticality.CRITICAL,
                affected_requirement_ids=[req_id],
            ),
        )

    for first, second, reason in conflicts:
        _append(
            items,
            seen,
            ReviewItem(
                id=f"RV_conflict_{first}__{second}",
                kind="conflict",
                question=f"{first} and {second} conflict: {reason}. Which one should be kept?",
                options=[option.format(first=first, second=second) for option in _CONFLICT_OPTIONS],
                recommended=_KEEP_BOTH,
                blocking=True,
                affected_requirement_ids=[first, second],
            ),
        )

    for issue in validation.issues:
        if issue.severity != "warning" or issue.req_id is None:
            continue
        requirement = by_id.get(issue.req_id)
        if requirement is None or requirement.criticality is not Criticality.CRITICAL:
            continue
        options, recommended = _AMBIGUITY_GUIDANCE.get(issue.code, _DEFAULT_AMBIGUITY)
        _append(
            items,
            seen,
            ReviewItem(
                id=f"RV_ambiguity_{issue.req_id}_{issue.code}",
                kind="ambiguity",
                question=(
                    f"{issue.message} (requirement {issue.req_id}, code {issue.code}). "
                    f"How should this be resolved?"
                ),
                options=list(options),
                recommended=recommended,
                blocking=False,
                affected_requirement_ids=[issue.req_id],
            ),
        )

    return ReviewOutcome(
        verified=verified,
        unverified=unverified,
        conflicts=conflict_ids,
        items=items,
        issues=[*validation.issues, *verification_issues],
    )


def _append(items: list[ReviewItem], seen: set[str], item: ReviewItem) -> None:
    """Append unless an item with this id is already there (duplicate req_ids)."""
    if item.id in seen:
        return
    seen.add(item.id)
    items.append(item)


def apply_review(requirements: Sequence[Requirement], outcome: ReviewOutcome) -> list[Requirement]:
    """Write the review's honest defaults onto copies of the requirements.

    * ``DOCUMENT`` requirements get ``citation_verified`` from the review: true
      only for verified citations, ``False`` for unverified ones. An unverified
      requirement keeps its expression — the engine's gate already turns
      ``citation_verified=False`` into UNKNOWN, so deleting the expression would
      destroy information without changing any verdict.
    * Conflicting requirements get ``status="conflict"`` and
      ``conflicts=[<other req_id>: <reason>]``. Conflicts are recomputed from the
      given list (the outcome carries the ids, the pairs carry the reasons), and
      entries already present on the requirement are kept ahead of the new ones,
      so applying the same outcome twice changes nothing.

    ``TEST_FIXTURE``/``USER`` requirements keep their ``citation_verified`` value:
    it is not a meaningful field for data that was never cited from a document.
    """
    ordered = list(requirements)
    verified = set(outcome.verified)
    unverified = set(outcome.unverified)
    marked = set(outcome.conflicts)
    entries: dict[str, list[str]] = {}
    for first, second, reason in find_conflicts(ordered):
        entries.setdefault(first, []).append(f"{second}: {reason}")
        entries.setdefault(second, []).append(f"{first}: {reason}")

    updated: list[Requirement] = []
    for requirement in ordered:
        changes: dict[str, object] = {}
        if requirement.origin is RequirementOrigin.DOCUMENT:
            if requirement.req_id in verified:
                changes["citation_verified"] = True
            elif requirement.req_id in unverified:
                changes["citation_verified"] = False
        detected = entries.get(requirement.req_id, [])
        if detected or requirement.req_id in marked:
            changes["status"] = "conflict"
            changes["conflicts"] = _merge_conflicts(requirement.conflicts, detected)
        updated.append(requirement.model_copy(update=changes) if changes else requirement)
    return updated


def _merge_conflicts(existing: Sequence[str], detected: Sequence[str]) -> list[str]:
    merged = list(dict.fromkeys(existing))
    for entry in detected:
        if entry not in merged:
            merged.append(entry)
    return merged
