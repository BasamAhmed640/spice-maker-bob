"""Provider contract for datasheet extraction (D11).

A provider turns document snippets into a structured payload. The contract is
deliberately small so that every implementation — the committed fixture replay
used by all automated tests, and the later HTTP/Bob adapters — is
interchangeable, and so that the pipeline records *which* provider ran and in
which native usage units (:class:`~boardmodeler.domain.records.ProviderIdentity`)
instead of assuming anything.

Nothing in this module performs I/O. Usage is reported in provider-native units
(``tokens`` for HTTP/Bob direct, ``turns`` for Bob Shell, ``none`` for fixture
replay) and is never converted between them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes
from boardmodeler.domain.records import DocumentRecord, ProviderIdentity

__all__ = [
    "MAX_SNIPPET_CHARS",
    "DocSnippet",
    "ExtractionRequest",
    "ExtractionResponse",
    "ExtractionTask",
    "Provider",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderHealth",
    "build_prompt",
    "request_hash",
]

MAX_SNIPPET_CHARS = 8000
"""Hard cap on one page's text, matching deterministic page-level chunking."""


class ProviderError(RuntimeError):
    """A provider failure with a machine-readable code.

    ``code`` is the stable identifier callers switch on (``fixture_missing``,
    ``cancelled``, ``bob_credentials_unavailable``, ``provider_not_implemented``,
    ...); ``detail`` is the human-readable observed reason. Both are attributes,
    so a stage can record the code in a manifest without parsing the message.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can do, stated before it is used."""

    structured_output: bool
    max_snippet_chars_pages: int
    streaming: bool
    usage_units: Literal["tokens", "turns", "requests", "none"]
    notes: str = ""


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider health probe.

    ``ok=False`` always carries a machine-readable ``code`` and the observed
    ``detail``; selection never treats an unhealthy provider as usable.
    """

    ok: bool
    code: str
    detail: str
    latency_ms: float | None = None


@dataclass(frozen=True)
class DocSnippet:
    """One page of extracted document text, with its 0-based PDF page number."""

    doc_id: str
    pdf_page: int
    printed_label: str | None
    text: str

    def __post_init__(self) -> None:
        if self.pdf_page < 0:
            raise ValueError(f"pdf_page is 0-based and must be >= 0, got {self.pdf_page}")
        if len(self.text) > MAX_SNIPPET_CHARS:
            raise ValueError(
                f"snippet for doc {self.doc_id!r} page {self.pdf_page} has {len(self.text)} "
                f"characters; the maximum is {MAX_SNIPPET_CHARS}"
            )


class ExtractionTask(StrEnum):
    """The four extraction jobs the pipeline requests."""

    IDENTITY = "IDENTITY"
    PINMAP = "PINMAP"
    REQUIREMENTS = "REQUIREMENTS"
    CAPABILITY_SUMMARY = "CAPABILITY_SUMMARY"


@dataclass(frozen=True)
class ExtractionRequest:
    """Everything a provider is allowed to see for one call.

    ``documents`` carries the records (classification, egress flags) for policy
    checks; ``allow_remote`` is the explicit per-call consent. Neither changes
    the cache key, because neither changes what the provider sees.
    """

    task: ExtractionTask
    prompt: str
    schema_json: str
    snippets: tuple[DocSnippet, ...]
    documents: tuple[DocumentRecord, ...] = ()
    allow_remote: bool = False


@dataclass(frozen=True)
class ExtractionResponse:
    """One extraction result.

    ``identity`` records the provider/model and native usage units; ``from_cache``
    is True only when an earlier identical request produced this payload without
    contacting the provider again.
    """

    payload: dict[str, Any]
    identity: ProviderIdentity
    raw_text: str
    from_cache: bool
    request_hash: str
    detail: str = ""


class Provider(Protocol):
    """The four calls the pipeline makes on any extraction provider."""

    def identity(self) -> ProviderIdentity: ...

    def capabilities(self) -> ProviderCapabilities: ...

    def health(self, timeout_s: float) -> ProviderHealth: ...

    def extract(
        self, request: ExtractionRequest, cancel: threading.Event | None = None
    ) -> ExtractionResponse: ...


def request_hash(
    request: ExtractionRequest, *, provider: str, model: str | None, context: str | None = None
) -> str:
    """Content-addressed cache key for one extraction call.

    Hashes every input that changes what a provider would see — provider and
    model identity, task, prompt, JSON schema, and the ordered snippets
    (doc id, 0-based page, text) — so a change to any of them yields a different
    key and can never silently reuse an answer produced under other inputs.
    """
    payload = {
        "provider": provider,
        "model": model,
        "task": request.task.value,
        "prompt": request.prompt,
        "schema_json": request.schema_json,
        "snippets": [
            {"doc_id": snippet.doc_id, "pdf_page": snippet.pdf_page, "text": snippet.text}
            for snippet in request.snippets
        ],
    }
    if context is not None:
        payload["adapter_context"] = context
    return sha256_bytes(canonical_json_bytes(payload))


_TASK_FOCUS: dict[ExtractionTask, str] = {
    ExtractionTask.IDENTITY: (
        "Identify the device: manufacturer, family, ordering code, base part, package, "
        "revision, and list anything the pages leave ambiguous."
    ),
    ExtractionTask.PINMAP: (
        "Extract the pin map: every physical pin with its name, function, direction, "
        "supply domain, and connection requirement."
    ),
    ExtractionTask.REQUIREMENTS: (
        "Extract falsifiable electrical, functional, temporal, and connectivity "
        "requirements with their limits, conditions, and evidence."
    ),
    ExtractionTask.CAPABILITY_SUMMARY: (
        "Summarise what the pages say about modelling-relevant behaviour and where they are silent."
    ),
}


def build_prompt(task: ExtractionTask, schema_json: str) -> str:
    """Deterministic instruction template for one extraction task.

    The rules are part of the contract, not decoration: an invented citation or
    a guessed value would later be recorded as evidence the pipeline cannot
    verify, so the prompt states exactly what to do instead — emit ``UNKNOWN``
    with the missing information named, and copy excerpts verbatim.
    """
    return (
        f"You are extracting structured data from engineering documents. Task: {task.value}.\n"
        f"{_TASK_FOCUS[task]}\n"
        "\n"
        "Rules:\n"
        "1. Use only the supplied pages. Never use outside knowledge for a value you emit.\n"
        "2. Never invent or paraphrase a citation: every excerpt must be copied verbatim "
        "from the supplied page text, and every page number must be one of the supplied "
        "pages.\n"
        '3. When the document does not state something, emit the literal value "UNKNOWN" '
        "(or null where the schema allows it) and say what is missing. Do not guess.\n"
        "4. Do not upgrade a typical value to a guaranteed limit, and do not treat "
        "absolute-maximum ratings as operating limits.\n"
        "5. Respond with a single JSON object matching this schema, and nothing else:\n"
        f"{schema_json}\n"
    )
