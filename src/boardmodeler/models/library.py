"""Vendor/generated model store (D8).

Rules this module enforces:

* A vendor original is stored **byte-identical** and never edited; its sha256 is
  recorded, and every adapted artifact carries the ``original_sha256`` it came
  from plus an :class:`~boardmodeler.models.adapt.AdaptationReport`.
* Generated (type-B) artifacts and adapted (ported) artifacts live in separate
  files from the original, so a reader can always tell which is which.
* A record is verified by hashing the stored bytes — never by trusting the
  filename.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from boardmodeler.domain.hashing import sha256_bytes, sha256_file, sha256_text
from boardmodeler.models.adapt import AdaptationReport
from boardmodeler.security.paths import resolve_within, safe_filename

__all__ = [
    "ModelKindOnDisk",
    "ModelRecord",
    "ModelStore",
    "ModelStoreError",
    "subckt_name",
    "subckt_ports",
    "subckts_in_text",
]

ModelKindOnDisk = Literal["vendor_original", "vendor_adapted", "generated", "primitive_library"]

_SUBCKT_RE = re.compile(r"(?im)^\s*\.subckt\s+(\S+)([^\n]*)")


class ModelStoreError(RuntimeError):
    """Raised for a missing model or an integrity failure."""


class ModelRecord(BaseModel):
    """One stored model artifact."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    kind: ModelKindOnDisk
    path: str
    sha256: str
    size: int
    subckts: list[str] = Field(default_factory=list)
    file_type: Literal["lib", "sub", "txt", "asy"] = "lib"
    license_note: str | None = None
    source_url: str | None = None
    original_sha256: str | None = None
    adaptation: AdaptationReport | None = None
    created_utc: str = ""
    notes: list[str] = Field(default_factory=list)
    immutable: bool = False

    def absolute(self, root: str | Path) -> Path:
        return Path(root) / self.path


def subckts_in_text(text: str) -> list[str]:
    """Every ``.subckt`` name declared in SPICE text, in file order."""
    return [m.group(1) for m in _SUBCKT_RE.finditer(text)]


def subckt_name(text: str) -> str | None:
    """The first ``.subckt`` name, or ``None``."""
    names = subckts_in_text(text)
    return names[0] if names else None


def subckt_ports(text: str, name: str | None = None) -> tuple[str, ...]:
    """Port names of a subcircuit, in declaration order.

    Continuation lines (``+``) are folded first, because a long port list is
    routinely split across lines in vendor models.
    """
    target = name or subckt_name(text)
    if target is None:
        return ()
    lines = text.splitlines()
    collected: str | None = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        match = _SUBCKT_RE.match(stripped)
        if match and match.group(1).lower() == target.lower():
            collected = stripped
            for follow in lines[index + 1 :]:
                follow_stripped = follow.strip()
                if follow_stripped.startswith("+"):
                    collected += " " + follow_stripped[1:].strip()
                    continue
                break
            break
    if collected is None:
        raise ModelStoreError(f"subcircuit {target!r} not found")
    body = _SUBCKT_RE.match(collected)
    assert body is not None
    params_index = collected.lower().find("params:")
    tail = collected[:params_index] if params_index != -1 else collected
    tokens = tail.split()[2:]
    node_tokens = [t for t in tokens if "=" not in t]
    return tuple(node_tokens)


class ModelStore:
    """Content-addressed store of model artifacts inside a project."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        for sub in ("models/vendor", "models/generated", "models/records"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def _relative(self, kind: ModelKindOnDisk, model_id: str, suffix: str) -> str:
        folder = {
            "vendor_original": "models/vendor",
            "vendor_adapted": "models/vendor",
            "generated": "models/generated",
            "primitive_library": "models/generated",
        }[kind]
        return f"{folder}/{safe_filename(model_id)}{suffix}"

    # ------------------------------------------------------------------- add

    def add_vendor_original(
        self,
        source: str | Path,
        *,
        model_id: str,
        license_note: str,
        source_url: str | None = None,
        notes: list[str] | None = None,
    ) -> ModelRecord:
        """Copy a vendor artifact byte-identically and record its hash."""
        source_path = Path(source)
        data = source_path.read_bytes()
        digest = sha256_bytes(data)
        existing = self._existing_immutable(model_id, "vendor_original", data)
        if existing is not None:
            return existing
        target_rel = self._relative("vendor_original", model_id, source_path.suffix or ".lib")
        target = resolve_within(self.root, target_rel)
        # Exclusive creation also protects an unrecorded original from accidental
        # replacement. An identical existing file may acquire its missing receipt.
        try:
            with target.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if target.read_bytes() != data:
                raise ModelStoreError(
                    f"immutable vendor original {model_id!r} already exists"
                ) from None
        text = _decode(data)
        record = ModelRecord(
            model_id=model_id,
            kind="vendor_original",
            path=target_rel.replace("\\", "/"),
            sha256=digest,
            size=len(data),
            subckts=subckts_in_text(text),
            file_type=_file_type(source_path.suffix),
            license_note=license_note,
            source_url=source_url or str(source_path),
            created_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            notes=list(notes or []),
            immutable=True,
        )
        self._write_record(record)
        return record

    def add_text_artifact(
        self,
        text: str,
        *,
        model_id: str,
        kind: ModelKindOnDisk,
        file_type: Literal["lib", "sub", "txt", "asy"] = "lib",
        license_note: str | None = None,
        source_url: str | None = None,
        original_sha256: str | None = None,
        adaptation: AdaptationReport | None = None,
        notes: list[str] | None = None,
    ) -> ModelRecord:
        """Store generated/adapted text with its provenance."""
        if kind == "vendor_original":
            raise ModelStoreError("vendor originals must use add_vendor_original with exact bytes")
        existing = self._existing_immutable(model_id, kind, text.encode("utf-8"))
        if existing is not None:
            return existing
        digest = sha256_text(text)
        target_rel = self._relative(kind, model_id, f".{file_type}")
        target = resolve_within(self.root, target_rel)
        target.write_bytes(text.encode("utf-8"))
        record = ModelRecord(
            model_id=model_id,
            kind=kind,
            path=target_rel.replace("\\", "/"),
            sha256=digest,
            size=len(text.encode("utf-8")),
            subckts=subckts_in_text(text),
            file_type=file_type,
            license_note=license_note,
            source_url=source_url,
            original_sha256=original_sha256,
            adaptation=adaptation,
            created_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            notes=list(notes or []),
        )
        self._write_record(record)
        return record

    # ------------------------------------------------------------------ read

    def records(self) -> list[ModelRecord]:
        records_dir = self.root / "models" / "records"
        return [
            ModelRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(records_dir.glob("*.json"))
        ]

    def get(self, model_id: str) -> ModelRecord:
        path = resolve_within(self.root, f"models/records/{safe_filename(model_id)}.json")
        if not path.is_file():
            raise ModelStoreError(f"no model record {model_id!r} in {self.root}")
        try:
            record = ModelRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelStoreError(f"invalid stored model record {model_id!r}") from exc
        if record.model_id != model_id:
            raise ModelStoreError(f"stored model ID collision for {model_id!r}")
        return record

    def path_of(self, model_id: str) -> Path:
        return self.get(model_id).absolute(self.root)

    def read_text(self, model_id: str) -> str:
        return self.path_of(model_id).read_text(encoding="utf-8")

    def verify(self, model_id: str) -> bool:
        """True when the stored bytes still hash to the recorded digest."""
        record = self.get(model_id)
        path = record.absolute(self.root)
        if not path.is_file():
            return False
        return sha256_file(path) == record.sha256

    def export_copy(
        self, model_id: str, destination: str | Path, *, allow_vendor_original: bool = False
    ) -> Path:
        """Copy verified bytes; a vendor original requires explicit personal-delivery opt-in.

        The opt-in is not a redistribution-license claim. Historical board export
        callers retain the default refusal and must reference the local original.
        """
        record = self.get(model_id)
        if record.kind == "vendor_original" and not allow_vendor_original:
            raise ModelStoreError(
                f"{model_id!r} is a vendor original and is not redistributed; export references "
                "it by the user's configured path instead"
            )
        data = self._verified_bytes(record)
        target = Path(destination)
        resolved_target = target.resolve()
        source = resolve_within(self.root, record.path)
        if resolved_target == source:
            return target
        if any(
            resolved_target.is_relative_to((self.root / folder).resolve())
            for folder in ("models/vendor", "models/records")
        ):
            raise ModelStoreError("export destination would overwrite protected model storage")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    # --------------------------------------------------------------- internal

    def _verified_bytes(self, record: ModelRecord) -> bytes:
        try:
            data = resolve_within(self.root, record.path).read_bytes()
        except (OSError, ValueError) as exc:
            raise ModelStoreError(f"stored model {record.model_id!r} is unavailable") from exc
        if len(data) != record.size or sha256_bytes(data) != record.sha256:
            raise ModelStoreError(f"stored model {record.model_id!r} failed integrity verification")
        return data

    def _existing_immutable(
        self, model_id: str, kind: ModelKindOnDisk, data: bytes
    ) -> ModelRecord | None:
        path = resolve_within(self.root, f"models/records/{safe_filename(model_id)}.json")
        if not path.exists():
            return None
        record = self.get(model_id)
        if record.immutable or record.kind == "vendor_original":
            self._verified_bytes(record)
            if not record.immutable or kind != record.kind or sha256_bytes(data) != record.sha256:
                raise ModelStoreError(f"immutable vendor original {model_id!r} cannot be replaced")
            return record
        if kind == "vendor_original":
            raise ModelStoreError(f"model ID {model_id!r} is already registered as {record.kind}")
        return None

    def _write_record(self, record: ModelRecord) -> None:
        path = resolve_within(self.root, f"models/records/{safe_filename(record.model_id)}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        if record.kind == "vendor_original":
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(record.model_dump_json(indent=2))
                return
            except FileExistsError:
                self._existing_immutable(record.model_id, record.kind, self._verified_bytes(record))
                return
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def _file_type(suffix: str) -> Literal["lib", "sub", "txt", "asy"]:
    suffix = suffix.lower().lstrip(".")
    if suffix in ("lib", "sub", "asy", "txt"):
        return suffix  # type: ignore[return-value]
    return "lib"


def _decode(data: bytes) -> str:
    if len(data) >= 2 and data[1] == 0:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")
