"""Datasheet-to-requirements extraction (Phase 4 steps 2-3).

One extraction runs the four provider tasks over the project's documents,
chunked page by page:

| task | payload | parsed into |
|---|---|---|
| ``IDENTITY`` | ``{"part": {...}}`` | :class:`~boardmodeler.domain.records.PartIdentity` |
| ``PINMAP`` | ``{"part_id": ..., "pins": [...]}`` | :class:`~boardmodeler.domain.records.PinDefinition` list |
| ``REQUIREMENTS`` | ``{"requirements": [...]}`` | :class:`~boardmodeler.domain.records.Requirement` list |
| ``CAPABILITY_SUMMARY`` | ``{"behaviors": {...}}`` | reporting-only ``dict[str, str]`` |

Rules the module enforces, because the pipeline's honesty depends on them:

* **Strict parsing.** A payload that does not validate against the D4 schemas is
  a failed extraction (`extraction_payload_invalid`), never a partially accepted
  answer. No field is defaulted by this module.
* **Explicit egress.** Before a document is sent to a *remote* provider (anything
  that is not the offline fixture replay) it is checked with
  :func:`boardmodeler.security.policy.evaluate_egress`; a denied document is not
  sent and produces a ``extract_egress_denied`` :class:`Finding` carrying the
  policy's machine-readable reason. The fixture provider never consults the
  policy because it never leaves the machine.
* **No capability claims.** ``CAPABILITY_SUMMARY`` is reporting material only. It
  is deliberately *not* merged into a :class:`ModelCapability` record — a
  capability claim requires a probe (Phase 2 step 6).
* **Zero requests on replay.** Responses are content-addressed by
  :func:`boardmodeler.providers.base.request_hash` and served from
  :class:`boardmodeler.providers.cache.ExtractionCache` when the same request was
  already answered.
* Citation verification is left to :func:`boardmodeler.requirements.review.review`;
  the extracted ``citation_verified`` values are not trusted, and
  ``review.apply_review`` (called by the pipeline's BUILD_REQUIREMENTS stage)
  is what writes the verified result onto the requirements.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, ValidationError, create_model

from boardmodeler.config import load_config
from boardmodeler.documents.chunk import chunk_document, select_pages
from boardmodeler.documents.pdf import PdfDocument, page_text, read_pdf
from boardmodeler.domain.enums import ProviderKind, Status
from boardmodeler.domain.records import (
    DataDisclosure,
    DocumentRecord,
    Finding,
    PartIdentity,
    PinDefinition,
    ProviderIdentity,
    Requirement,
)
from boardmodeler.pipeline.project import Project
from boardmodeler.providers.base import (
    MAX_SNIPPET_CHARS,
    DocSnippet,
    ExtractionRequest,
    ExtractionResponse,
    ExtractionTask,
    Provider,
    ProviderError,
    build_prompt,
    request_hash,
)
from boardmodeler.providers.cache import ExtractionCache
from boardmodeler.requirements.model import RequirementIssue, validate_requirements
from boardmodeler.requirements.review import ReviewOutcome, review
from boardmodeler.security.policy import DataPolicy, build_disclosure, evaluate_egress

__all__ = [
    "ExtractionResult",
    "extract_requirements",
    "schema_json",
    "tasks_for",
]

_TEXT_SUFFIXES = frozenset({".txt", ".text", ".md"})
_BEHAVIOR_STATES = ("supported", "unsupported", "unknown", "not_tested")
_MAX_VALIDATION_DETAIL = 700


# --------------------------------------------------------------------------- #
# requests


def schema_json(task: ExtractionTask) -> str:
    """Deterministic JSON schema text for one task.

    The schema is part of the request hash, so changing a schema changes every
    cache key — an old answer can never be replayed against a new contract. The
    pydantic D4 records remain the authority; the schema tells the model the
    shape.
    """
    return json.dumps(_SCHEMAS[task], indent=2, sort_keys=True, ensure_ascii=False)


def tasks_for(
    task: ExtractionTask,
    snippets: Sequence[DocSnippet],
    *,
    documents: Sequence[DocumentRecord] = (),
    allow_remote: bool = False,
) -> ExtractionRequest:
    """The exact request an extraction provider is asked for one task."""
    schema = schema_json(task)
    return ExtractionRequest(
        task=task,
        prompt=build_prompt(task, schema),
        schema_json=schema,
        snippets=tuple(snippets),
        documents=tuple(documents),
        allow_remote=allow_remote,
    )


# --------------------------------------------------------------------------- #
# result


@dataclass(frozen=True)
class ExtractionResult:
    """Everything one extraction produced, with the questions it left open."""

    requirements: list[Requirement]
    pins: list[PinDefinition]
    part: PartIdentity | None
    capability_summary: dict[str, str]
    provider_identity: ProviderIdentity
    disclosures: list[DataDisclosure]
    cache_hits: int
    issues: list[RequirementIssue]
    review: ReviewOutcome
    detail: str
    #: Extraction-level problems that are not tied to a requirement (denied
    #: egress, an unreadable document). The pipeline records these as findings.
    findings: list[Finding] = field(default_factory=list)


def extract_requirements(
    project: Project,
    *,
    provider: Provider,
    task_pages: Mapping[ExtractionTask | str, Sequence[int]] | None = None,
    max_chars: int = MAX_SNIPPET_CHARS,
    cache_dir: Path | None = None,
    policy: DataPolicy | None = None,
    allow_remote: bool | None = None,
    cancel: threading.Event | None = None,
) -> ExtractionResult:
    """Run the four extraction tasks over ``project``'s documents.

    ``cache_dir`` defaults to the project's ``evidence/cache``; pass an explicit
    directory to control it, or set ``DataPolicy.cache_extraction=False`` to
    disable reuse. ``allow_remote`` overrides the policy's flag for this call
    (the CLI's ``--allow-remote``); ``None`` means "use the policy".
    """
    policy = policy if policy is not None else load_config().data_policy
    remote = policy.allow_remote if allow_remote is None else bool(allow_remote)
    identity = provider.identity()
    remote_provider = identity.kind is not ProviderKind.FIXTURE

    records = {record.doc_id: record for record in project.documents()}
    findings, snippets_by_doc = _collect_snippets(project, records, max_chars=max_chars)

    codec = _PageLookup(project, records)

    sent_docs: list[DocumentRecord] = []
    allowed_snippets: list[DocSnippet] = []
    for doc_id in sorted(snippets_by_doc):
        record = records[doc_id]
        if remote_provider:
            decision = evaluate_egress(policy, record, allow_remote=remote)
            if not decision.allowed:
                findings.append(
                    Finding(
                        code="extract_egress_denied",
                        status=Status.BLOCKED,
                        message=(
                            f"document {doc_id} was not sent to provider "
                            f"{identity.provider!r}: {decision.reason}: {decision.detail}"
                        ),
                        detail={
                            "doc_id": doc_id,
                            "reason": decision.reason,
                            "classification": record.classification,
                        },
                    )
                )
                continue
        sent_docs.append(record)
        allowed_snippets.extend(snippets_by_doc[doc_id])

    cache = _make_cache(project, cache_dir, policy)
    if not snippets_by_doc:
        raise ProviderError(
            "no_documents",
            f"project {project.config.project_id!r} has no readable stored documents; "
            "nothing was sent to the provider",
        )
    if not allowed_snippets:
        reasons = "; ".join(f"{finding.code}: {finding.message}" for finding in findings)
        raise ProviderError(
            "egress_denied_no_documents",
            f"no document could be sent to provider {identity.provider!r}: {reasons}",
        )
    responses: dict[ExtractionTask, ExtractionResponse] = {}
    requests: dict[ExtractionTask, ExtractionRequest] = {}
    cache_hits = 0
    pending: list[ExtractionRequest] = []
    for task in ExtractionTask:
        task_snippets = select_pages(allowed_snippets, pages=_pages_for(task, task_pages))
        request = tasks_for(
            task,
            task_snippets,
            documents=sent_docs,
            allow_remote=remote and remote_provider,
        )
        requests[task] = request
        key = request_hash(
            request,
            provider=identity.provider,
            model=identity.model,
            context=getattr(provider, "cache_context", None),
        )

        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            cache_hits += 1
            responses[task] = ExtractionResponse(
                payload=cached,
                identity=identity,
                raw_text=json.dumps(cached, sort_keys=True),
                from_cache=True,
                request_hash=key,
                detail=f"served from cache entry {key}",
            )
            continue
        pending.append(request)

    batch = getattr(provider, "extract_many", None)
    fetched = batch(pending, cancel) if pending and callable(batch) else None
    for request in pending:
        task = request.task
        key = request_hash(
            request,
            provider=identity.provider,
            model=identity.model,
            context=getattr(provider, "cache_context", None),
        )
        response = fetched[task] if fetched is not None else provider.extract(request, cancel)
        if response.from_cache:
            cache_hits += 1
        if cache is not None:
            cache.put(
                key,
                response.payload,
                provider=identity.provider,
                model=identity.model,
                task=task,
                notes=f"provider={identity.provider} kind={identity.kind.value}",
            )
        responses[task] = response

    requirements = _parse_requirements(responses[ExtractionTask.REQUIREMENTS])
    pins = _parse_pins(responses[ExtractionTask.PINMAP])
    part = _parse_identity(responses[ExtractionTask.IDENTITY])
    behaviors = _parse_behaviors(responses[ExtractionTask.CAPABILITY_SUMMARY])

    validation = validate_requirements(requirements, documents=records)
    outcome = review(requirements, records, excerpt_lookup=codec.page_text)
    issues = _merge_issues(validation.issues, outcome.issues)

    disclosures = _disclosures(
        identity=identity,
        remote_provider=remote_provider and remote,
        policy=policy,
        snippets_by_doc={doc_id: snippets_by_doc[doc_id] for doc_id in sorted(snippets_by_doc)},
        sent_doc_ids={record.doc_id for record in sent_docs},
    )

    detail = (
        f"provider={identity.provider} tasks={len(responses)} cache_hits={cache_hits} "
        f"requirements={len(requirements)} pins={len(pins)} "
        f"part={'identified' if part is not None else 'unresolved'} "
        f"review_items={len(outcome.items)} issues={len(issues)}"
    )
    if identity.model:
        detail += f" model={identity.model}"

    return ExtractionResult(
        requirements=requirements,
        pins=pins,
        part=part,
        capability_summary=behaviors,
        provider_identity=identity,
        disclosures=disclosures,
        cache_hits=cache_hits,
        issues=issues,
        review=outcome,
        detail=detail,
        findings=findings,
    )


# --------------------------------------------------------------------------- #
# documents


def _collect_snippets(
    project: Project, records: Mapping[str, DocumentRecord], *, max_chars: int
) -> tuple[list[Finding], dict[str, list[DocSnippet]]]:
    """Chunk every stored document; an unreadable one becomes a finding, not silence."""
    findings: list[Finding] = []
    snippets_by_doc: dict[str, list[DocSnippet]] = {}
    for doc_id in sorted(records):
        record = records[doc_id]
        try:
            chunks = chunk_document(record, base_dir=project.root, max_chars=max_chars)
        except Exception as exc:  # a missing or malformed original is reported verbatim
            findings.append(
                Finding(
                    code="extract_document_unreadable",
                    status=Status.BLOCKED,
                    message=(
                        f"document {doc_id} could not be chunked and was not sent to the "
                        f"provider: {type(exc).__name__}: {exc}"
                    ),
                    detail={"doc_id": doc_id, "error": f"{type(exc).__name__}: {exc}"},
                )
            )
            continue
        snippets_by_doc[doc_id] = chunks
    return findings, snippets_by_doc


class _PageLookup:
    """Page text for citation verification, read from the project's own copies.

    ``DocumentRecord.path`` is project-relative by construction
    (:class:`boardmodeler.documents.store.DocumentStore` writes it that way), so
    the lookup resolves it against the project root instead of the process
    working directory. PDFs are read once per document and grown page by page on
    demand; a document that cannot be read yields ``None``, which the review
    records as "not verified" rather than as an exception.
    """

    def __init__(self, project: Project, records: Mapping[str, DocumentRecord]) -> None:
        self._project = project
        self._records = records
        self._pdfs: dict[str, PdfDocument] = {}
        self._texts: dict[str, str] = {}

    def page_text(self, doc_id: str, pdf_page: int) -> str | None:
        record = self._records.get(doc_id)
        if record is None or not record.path:
            return None
        path = Path(record.path)
        if not path.is_absolute():
            path = self._project.root / path
        if not path.is_file():
            return None
        if path.suffix.lower() in _TEXT_SUFFIXES:
            text = self._texts.get(doc_id)
            if text is None:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
                self._texts[doc_id] = text
            return text if pdf_page == 0 else None
        document = self._pdfs.get(doc_id)
        if document is None or pdf_page >= len(document.pages):
            try:
                document = read_pdf(path, max_pages=pdf_page + 1)
            except Exception:  # an unreadable PDF cannot verify anything
                return None
            self._pdfs[doc_id] = document
        if pdf_page >= len(document.pages):
            return None
        return page_text(document, pdf_page)


# --------------------------------------------------------------------------- #
# cache


def _make_cache(
    project: Project, cache_dir: Path | None, policy: DataPolicy
) -> ExtractionCache | None:
    if not policy.cache_extraction:
        return None
    root = Path(cache_dir) if cache_dir is not None else project.path("evidence/cache")
    return ExtractionCache(root)


def _pages_for(
    task: ExtractionTask, task_pages: Mapping[ExtractionTask | str, Sequence[int]] | None
) -> Sequence[int] | None:
    if not task_pages:
        return None
    for key, value in task_pages.items():
        if key is task:
            return value
        if isinstance(key, str) and key.strip().upper() == task.value:
            return value
    return None


# --------------------------------------------------------------------------- #
# payload parsing (strict)


def _parse_requirements(response: ExtractionResponse) -> list[Requirement]:
    payload = _require_object(response, ExtractionTask.REQUIREMENTS)
    raw = payload.get("requirements")
    if not isinstance(raw, list):
        raise _invalid(ExtractionTask.REQUIREMENTS, "'requirements' must be a list")
    return [
        _validate(
            Requirement, item, task=ExtractionTask.REQUIREMENTS, where=f"requirements[{index}]"
        )
        for index, item in enumerate(raw)
    ]


def _parse_pins(response: ExtractionResponse) -> list[PinDefinition]:
    payload = _require_object(response, ExtractionTask.PINMAP)
    raw = payload.get("pins")
    if not isinstance(raw, list):
        raise _invalid(ExtractionTask.PINMAP, "'pins' must be a list")
    part_id = payload.get("part_id")
    pins: list[PinDefinition] = []
    for index, item in enumerate(raw):
        where = f"pins[{index}]"
        if isinstance(item, dict) and "part_id" not in item and isinstance(part_id, str):
            item = {**item, "part_id": part_id}
        pins.append(_validate(PinDefinition, item, task=ExtractionTask.PINMAP, where=where))
    return pins


def _parse_identity(response: ExtractionResponse) -> PartIdentity | None:
    payload = _require_object(response, ExtractionTask.IDENTITY)
    raw = payload.get("part")
    if raw is None:
        return None
    return _validate(PartIdentity, raw, task=ExtractionTask.IDENTITY, where="part")


def _parse_behaviors(response: ExtractionResponse) -> dict[str, str]:
    payload = _require_object(response, ExtractionTask.CAPABILITY_SUMMARY)
    raw = payload.get("behaviors")
    if not isinstance(raw, Mapping):
        raise _invalid(ExtractionTask.CAPABILITY_SUMMARY, "'behaviors' must be an object")
    behaviors: dict[str, str] = {}
    for key, value in raw.items():
        state = str(value).strip().lower()
        if state not in _BEHAVIOR_STATES:
            raise _invalid(
                ExtractionTask.CAPABILITY_SUMMARY,
                f"behaviors[{key!r}] is {value!r}; expected one of {list(_BEHAVIOR_STATES)}",
            )
        behaviors[str(key)] = state
    return behaviors


def _require_object(response: ExtractionResponse, task: ExtractionTask) -> dict[str, Any]:
    payload = response.payload
    if not isinstance(payload, Mapping):
        raise _invalid(task, f"the payload is a {type(payload).__name__}, expected an object")
    return dict(payload)


def _validate(model: type[Any], data: object, *, task: ExtractionTask, where: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _invalid(task, f"{where} is not a valid {model.__name__}: {_brief(exc)}") from exc


def _invalid(task: ExtractionTask, detail: str) -> ProviderError:
    return ProviderError(
        "extraction_payload_invalid",
        f"{task.value}: {detail} (nothing from this payload was accepted)",
    )


def _brief(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:_MAX_VALIDATION_DETAIL] + ("..." if len(text) > _MAX_VALIDATION_DETAIL else "")


def _merge_issues(
    first: Sequence[RequirementIssue], second: Sequence[RequirementIssue]
) -> list[RequirementIssue]:
    """Validation issues plus review issues, without repeating an identical one."""
    merged: list[RequirementIssue] = []
    seen: set[tuple[str, str | None, str]] = set()
    for issue in [*first, *second]:
        key = (issue.code, issue.req_id, issue.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(issue)
    return merged


# --------------------------------------------------------------------------- #
# disclosure


def _disclosures(
    *,
    identity: ProviderIdentity,
    remote_provider: bool,
    policy: DataPolicy,
    snippets_by_doc: Mapping[str, Sequence[DocSnippet]],
    sent_doc_ids: set[str],
) -> list[DataDisclosure]:
    """One disclosure per document that left the machine (never for fixture replay)."""
    if not remote_provider:
        return []
    disclosures: list[DataDisclosure] = []
    for doc_id in sorted(snippets_by_doc):
        if doc_id not in sent_doc_ids:
            continue
        pages = sorted({snippet.pdf_page for snippet in snippets_by_doc[doc_id]})
        chars = sum(len(snippet.text) for snippet in snippets_by_doc[doc_id])
        disclosures.append(
            build_disclosure(
                provider=identity.provider,
                endpoint=identity.endpoint,
                page_ranges={doc_id: pages},
                chars=chars,
                policy=policy,
            )
        )
    return disclosures


# --------------------------------------------------------------------------- #
# schemas


def _record_schema(name: str, **fields) -> dict[str, Any]:
    # Generate evidence, expressions, enums and conditions from the actual parser.
    # Handwritten partial schemas used to make valid extraction impossible.
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields).model_json_schema(
        by_alias=True
    )


_SCHEMAS: dict[ExtractionTask, dict[str, Any]] = {
    ExtractionTask.IDENTITY: _record_schema("IdentityResult", part=(PartIdentity | None, ...)),
    ExtractionTask.PINMAP: _record_schema(
        "PinResult", part_id=(str | None, None), pins=(list[PinDefinition], ...)
    ),
    ExtractionTask.REQUIREMENTS: _record_schema(
        "RequirementsResult", requirements=(list[Requirement], ...)
    ),
    ExtractionTask.CAPABILITY_SUMMARY: {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "behaviors": {
                "type": "object",
                "additionalProperties": {"enum": list(_BEHAVIOR_STATES)},
            },
            "notes": {"type": "string"},
        },
        "required": ["behaviors"],
    },
}
