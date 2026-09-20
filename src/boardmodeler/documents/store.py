"""Content-addressed document store.

Layout (D3)::

    <project_dir>/docs/<doc_id>.json               DocumentRecord, indent=2 JSON
    <project_dir>/docs/files/<doc_id>-<name>       the bytes exactly as supplied

``doc_id`` is ``DOC_<sha256[:12]>_<slug>``: the hash prefix makes identical
content resolve to the same id, the slug keeps the id readable. Adding a file
that is already stored is idempotent; adding the same content under a different
stored name or with different metadata raises instead of silently discarding
the caller's arguments.

Originals are copied byte for byte and the copy is verified by hash, so a
document the store reports is a document that is really there. A
``doc_type="vendor_model"`` file is never parsed as a PDF — encrypted vendor
models are stored, not interpreted — and a synthetic fixture can never be
marked as allowed for remote inference.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import get_args

from boardmodeler.documents.pdf import read_pdf
from boardmodeler.domain.hashing import sha256_bytes, sha256_file
from boardmodeler.domain.ids import normalize_slug
from boardmodeler.domain.records import DocumentRecord
from boardmodeler.security.paths import resolve_within, safe_filename

__all__ = ["Classification", "DocType", "DocumentStore", "DocumentStoreError", "Provenance"]

# The record schema is the single source of truth for these vocabularies; the
# store uses the same sets so a bad value is rejected before anything is written.
DocType = DocumentRecord.model_fields["doc_type"].annotation
Provenance = DocumentRecord.model_fields["provenance"].annotation
Classification = DocumentRecord.model_fields["classification"].annotation

_DOC_TYPES = frozenset(get_args(DocType))
_PROVENANCE = frozenset(get_args(Provenance))
_CLASSIFICATIONS = frozenset(get_args(Classification))

_DOCS_DIR = "docs"
_FILES_DIR = "files"
_JSON_EXTENSION = "json"
_UNPARSED_DOC_TYPE = "vendor_model"
_DOC_ID_MAX_LEN = 120
_DOC_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SYNTHETIC_DOC_TYPE = "synthetic_contract"
_SYNTHETIC_PROVENANCE = "synthetic_fixture"


class DocumentStoreError(RuntimeError):
    """The store cannot honour a request: a conflicting record or a missing original."""


class DocumentStore:
    """The documents one project was analysed from, addressed by content hash."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir)
        self.docs_dir = self.project_dir / _DOCS_DIR
        self.files_dir = self.docs_dir / _FILES_DIR

    # ------------------------------------------------------------------ write

    def add_file(
        self,
        source: Path,
        *,
        doc_type: DocType,
        provenance: Provenance,
        classification: str = "unknown",
        remote_inference_allowed: bool = False,
        redistribution_allowed: bool = False,
        source_url: str | None = None,
        title: str | None = None,
        manufacturer: str | None = None,
        doc_id: str | None = None,
        update_remote_permission: bool = False,
    ) -> DocumentRecord:
        """Store ``source`` and return its record.

        The hash, page count, page labels and text-extraction state are read from
        the file itself; ``title`` falls back to the document's own title and then
        to the file name. Files whose ``doc_type`` is ``vendor_model`` are stored
        without any attempt to parse them.
        """
        _check_vocabulary(doc_type, _DOC_TYPES, "doc_type")
        _check_vocabulary(provenance, _PROVENANCE, "provenance")
        _check_vocabulary(classification, _CLASSIFICATIONS, "classification")
        original = Path(source)
        if not original.is_file():
            raise FileNotFoundError(f"no such document: {original}")

        file_hash = sha256_file(original)
        pdf_title: str | None = None
        page_count = 0
        page_labels: dict[int, str] = {}
        text_extraction = "none"
        if doc_type != _UNPARSED_DOC_TYPE:
            parsed = read_pdf(original)
            if parsed.file_hash != file_hash:
                raise DocumentStoreError(
                    f"{original} changed while it was being read: {file_hash[:12]} then "
                    f"{parsed.file_hash[:12]}"
                )
            pdf_title = parsed.title
            page_count = parsed.page_count
            page_labels = parsed.page_labels
            text_extraction = parsed.text_extraction

        effective_title = title or pdf_title or original.stem
        stored_id = _check_doc_id(doc_id or _make_doc_id(file_hash, effective_title))
        stored_name = _stored_name(stored_id, original.name)
        record = DocumentRecord(
            doc_id=stored_id,
            title=effective_title,
            manufacturer=manufacturer,
            doc_type=doc_type,
            file_hash=file_hash,
            source_url=source_url,
            provenance=provenance,
            classification=classification,
            remote_inference_allowed=remote_inference_allowed,
            page_count=page_count,
            page_labels=page_labels,
            text_extraction=text_extraction,
            path=f"{_DOCS_DIR}/{_FILES_DIR}/{stored_name}",
            redistribution_allowed=redistribution_allowed,
        )
        already = self._already_stored(record, update_remote_permission=update_remote_permission)
        if already is not None:
            return already
        destination = resolve_within(self.files_dir, stored_name)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        _copy_verified(original, destination, file_hash)
        self._write(record)
        return record

    def add_synthetic(
        self, name: str, text: str, *, doc_id: str | None = None, title: str | None = None
    ) -> DocumentRecord:
        """Store synthetic fixture text (a test contract, not device data).

        The text is written beside the record as a ``.txt`` file and presented as
        one logical page (page 0), so page-indexed evidence works the same way it
        does for a real document. The record is always ``public`` with
        ``remote_inference_allowed=False``: a fixture has no owner who could
        consent to sending it anywhere.
        """
        payload = text.encode("utf-8")
        file_hash = sha256_bytes(payload)
        effective_title = title or name
        stored_id = _check_doc_id(doc_id or _make_doc_id(file_hash, effective_title))
        stored_name = _source_stem(name) + ".txt"
        full_name = f"{stored_id}-{stored_name}"
        record = DocumentRecord(
            doc_id=stored_id,
            title=effective_title,
            doc_type=_SYNTHETIC_DOC_TYPE,
            file_hash=file_hash,
            provenance=_SYNTHETIC_PROVENANCE,
            classification="public",
            remote_inference_allowed=False,
            page_count=1,
            page_labels={},
            text_extraction="embedded",
            path=f"{_DOCS_DIR}/{_FILES_DIR}/{full_name}",
        )
        already = self._already_stored(record)
        if already is not None:
            return already
        destination = resolve_within(self.files_dir, full_name)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if sha256_file(destination) != file_hash:
            destination.unlink(missing_ok=True)
            raise DocumentStoreError(f"could not write {destination} byte for byte")
        self._write(record)
        return record

    # ------------------------------------------------------------------- read

    def get(self, doc_id: str) -> DocumentRecord:
        """Return the stored record for ``doc_id``.

        Raises ``KeyError`` when the id is well formed but not stored, and
        ``ValueError`` when it could not name a stored document at all.
        """
        path = self._record_path(doc_id)
        if not path.is_file():
            raise KeyError(f"no document {doc_id!r} in {self.docs_dir}")
        return DocumentRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def records(self) -> list[DocumentRecord]:
        """Every stored record, ordered by ``doc_id``.

        Any ``*.json`` in the docs directory must be a record: a foreign or
        corrupt file raises instead of being silently skipped, because silently
        skipping is how a lost document goes unnoticed.
        """
        if not self.docs_dir.is_dir():
            return []
        return [
            DocumentRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.docs_dir.glob(f"*.{_JSON_EXTENSION}"))
        ]

    def original_path(self, doc_id: str) -> Path:
        """Path of the stored copy of ``doc_id``'s bytes."""
        record = self.get(doc_id)
        path = resolve_within(self.project_dir, record.path)
        if not path.is_file():
            raise DocumentStoreError(f"{doc_id} records {record.path}, but {path} is not there")
        return path

    # -------------------------------------------------------------- internals

    def _record_path(self, doc_id: str) -> Path:
        return resolve_within(self.docs_dir, f"{_check_doc_id(doc_id)}.{_JSON_EXTENSION}")

    def _already_stored(
        self, record: DocumentRecord, *, update_remote_permission: bool = False
    ) -> DocumentRecord | None:
        """Return the identical stored record, or raise when it differs."""
        path = self._record_path(record.doc_id)
        if not path.is_file():
            return None
        stored = self.get(record.doc_id)
        if stored.file_hash != record.file_hash:
            raise DocumentStoreError(
                f"doc_id {record.doc_id} already holds different content "
                f"({stored.file_hash[:12]} != {record.file_hash[:12]})"
            )
        if stored != record:
            # A fresh user decision may grant or revoke egress without changing the
            # document's identity. All content and other metadata remain immutable.
            permission_only = stored.model_copy(
                update={"remote_inference_allowed": record.remote_inference_allowed}
            )
            if update_remote_permission and permission_only == record:
                self._write(record)
                return record
            raise DocumentStoreError(
                f"doc_id {record.doc_id} is already stored with different metadata; "
                "pass a distinct doc_id instead of reusing this one"
            )
        return stored

    def _write(self, record: DocumentRecord) -> Path:
        path = self._record_path(record.doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        return path


def _check_vocabulary(value: str, allowed: frozenset[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")


def _check_doc_id(doc_id: str) -> str:
    if len(doc_id) > _DOC_ID_MAX_LEN or not _DOC_ID_PATTERN.fullmatch(doc_id):
        raise ValueError(
            f"doc_id {doc_id!r} must be 1-{_DOC_ID_MAX_LEN} characters of [A-Za-z0-9._-] "
            "starting with a letter or a digit"
        )
    return doc_id


def _make_doc_id(file_hash: str, title: str) -> str:
    slug = normalize_slug(title, max_len=40, fallback="document")
    return f"DOC_{file_hash[:12]}_{slug}"


def _source_stem(name: str) -> str:
    cleaned = safe_filename(Path(name).name, fallback="fixture", max_len=60)
    return cleaned[: -len(".txt")] if cleaned.lower().endswith(".txt") else cleaned


def _stored_name(doc_id: str, source_name: str) -> str:
    return f"{doc_id}-{safe_filename(source_name, fallback='document', max_len=80)}"


def _copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    """Copy the bytes and prove the copy matches; never leave a partial original."""
    shutil.copyfile(source, destination)
    copied = sha256_file(destination)
    if copied != expected_hash:
        destination.unlink(missing_ok=True)
        raise DocumentStoreError(
            f"the copy of {source} at {destination} is not byte-identical "
            f"({copied[:12]} != {expected_hash[:12]})"
        )
