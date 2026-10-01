"""Acquire official manufacturer SPICE bytes without adapting or executing downloads.

An acquired bundle is evidence, not a compatibility or electrical PASS. The caller
must run the chosen original in its configured LTspice and independent test harness.
This bounded text/ZIP route does not accept executable installers or encrypted models.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import time
import urllib.parse
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.request import Request, build_opener

from boardmodeler.authoring.deck_policy import _logical_lines, _tokens
from boardmodeler.authoring.reinforce import (
    FetchRefused,
    _outside_vendor_reason,
    _RedirectCap,
    _require_public_host,
    official_hosts_for_url,
)
from boardmodeler.security.network import NetworkRefused, require_network
from boardmodeler.security.paths import safe_filename

_MODEL_SUFFIXES = frozenset({".lib", ".sub", ".cir", ".mod", ".sp", ".spice", ".txt"})
_FORBIDDEN_SUFFIXES = frozenset(
    {".exe", ".dll", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".scr", ".py"}
)
_MAX_DOWNLOAD = 32 * 1024 * 1024
_MAX_MEMBER = 8 * 1024 * 1024
_MAX_FILES = 200
_MAX_EXPANDED = 32 * 1024 * 1024
_TYPES = frozenset(
    {"text/plain", "application/octet-stream", "application/zip", "application/x-zip-compressed"}
)
_ENCRYPTED = re.compile(
    r"(?im)^\s*(?:\.encrypt\b|\.prot(?:ect|ected)?\b|\$?begin\s+encrypted\b|\$CDNENCSTART\b|\*\s*encrypted\s+model\b)"
)
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_+./-]{1,128}\Z")
_DIRECTIVES = frozenset(
    {
        ".subckt",
        ".ends",
        ".model",
        ".param",
        ".params",
        ".func",
        ".global",
        ".options",
        ".option",
        ".include",
        ".inc",
        ".lib",
        ".endl",
        ".ic",
        ".nodeset",
        ".temp",
        ".end",
        ".title",
        ".tran",
        ".ac",
        ".dc",
        ".op",
        ".meas",
        ".measure",
        ".probe",
        ".save",
        ".step",
        ".four",
        ".noise",
        ".tf",
        ".pz",
        ".sens",
        ".width",
    }
)


class OfficialSpiceError(ValueError):
    """An honest acquisition refusal, never permission to generate a replacement silently."""


@dataclass(frozen=True)
class OfficialSpiceFile:
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class OfficialSpiceEntry:
    kind: str
    name: str
    ports: tuple[str, ...]
    file: Path
    model_type: str | None = None


@dataclass(frozen=True)
class OfficialSpiceBundle:
    source_url: str
    source_sha256: str
    bundle_dir: Path
    files: tuple[OfficialSpiceFile, ...]
    entries: tuple[OfficialSpiceEntry, ...]
    final_url: str


@dataclass(frozen=True)
class OfficialModelLink:
    url: str
    label: str
    native_ltspice: bool


def _authority(allowed_hosts: Sequence[str]) -> tuple[str, ...]:
    """Validate caller-selected authority against the application manufacturer catalog."""
    origins: list[str] = []
    for host in allowed_hosts:
        selected = official_hosts_for_url(f"https://{host}/")
        if not selected:
            raise OfficialSpiceError("manufacturer_authority_invalid: unknown manufacturer origin")
        for origin in selected:
            if origin not in origins:
                origins.append(origin)
    if not origins:
        raise OfficialSpiceError(
            "manufacturer_authority_missing: no official manufacturer selected"
        )
    return tuple(origins)


def _download(
    url: str,
    *,
    allowed_hosts: Sequence[str],
    timeout_s: float,
    max_bytes: int,
    accepted_types: Sequence[str],
) -> tuple[bytes, str, str]:
    try:
        require_network("official manufacturer model retrieval")
    except NetworkRefused as exc:
        raise OfficialSpiceError(exc.detail) from exc
    origins = _authority(allowed_hosts)
    if not 0 < timeout_s <= 20 or not 0 < max_bytes <= _MAX_DOWNLOAD:
        raise OfficialSpiceError(
            "acquisition_budget_invalid: timeout or size outside bounded limits"
        )
    refusal = _outside_vendor_reason(url, origins)
    if refusal:
        raise OfficialSpiceError(refusal)
    deadline = time.monotonic() + timeout_s
    try:
        _require_public_host(url)
    except FetchRefused as exc:
        raise OfficialSpiceError(str(exc)) from exc
    request = Request(
        url,
        headers={
            "User-Agent": "SpiceMaker/1.8 official-model",
            "Accept": ", ".join(accepted_types),
        },
    )
    try:
        opener = build_opener(_RedirectCap(origins))
        left = deadline - time.monotonic()
        if left <= 0:
            raise OfficialSpiceError(
                "official_download_timeout: authority resolution exceeded budget"
            )
        with opener.open(request, timeout=left) as response:
            media_type = response.headers.get_content_type().lower()
            if media_type not in accepted_types:
                raise OfficialSpiceError(f"content_type_refused: {media_type}")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise OfficialSpiceError("download_too_large: declared size exceeds limit")
            final_url = response.geturl()
            refusal = _outside_vendor_reason(final_url, origins)
            if refusal:
                raise OfficialSpiceError(refusal)
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OfficialSpiceError("official_download_timeout: response exceeded budget")
                # urllib's HTTPResponse uses a buffered socket. Reduce its timeout as
                # the stage budget is spent, so a slow stream cannot renew that budget.
                stream_socket = getattr(
                    getattr(getattr(response, "fp", None), "raw", None), "_sock", None
                )
                if stream_socket is not None:
                    stream_socket.settimeout(left)
                read = getattr(response, "read1", response.read)
                chunk = read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
    except OfficialSpiceError:
        raise
    except Exception as exc:
        # Do not echo arbitrary server exception text or secret-bearing URLs.
        raise OfficialSpiceError(f"official_download_failed: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise OfficialSpiceError("download_too_large: observed size exceeds limit")
    return data, media_type, final_url


def fetch_official_bytes(
    url: str,
    *,
    allowed_hosts: Sequence[str],
    timeout_s: float = 20,
    max_bytes: int = 2 * 1024 * 1024,
    accepted_types: Sequence[str] = ("text/html", "text/plain"),
) -> tuple[bytes, str]:
    """Bounded manufacturer page retrieval, with the same authority on every redirect."""
    data, media_type, _final = _download(
        url,
        allowed_hosts=allowed_hosts,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        accepted_types=accepted_types,
    )
    return data, media_type


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.label = [dict(attrs).get("title") or ""]

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.href:
            self.links.append((self.href, " ".join(" ".join(self.label).split())))
            self.href = None
            self.label = []


def discover_model_links(
    product_url: str, html_bytes: bytes, *, allowed_hosts: Sequence[str]
) -> tuple[OfficialModelLink, ...]:
    """Actual manufacturer-page links; native LTspice labels precede other SPICE candidates."""
    origins = _authority(allowed_hosts)
    if _outside_vendor_reason(product_url, origins):
        raise OfficialSpiceError("manufacturer_page_refused: page is outside selected authority")
    if len(html_bytes) > 2 * 1024 * 1024:
        raise OfficialSpiceError("manufacturer_page_too_large")
    parser = _Links()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    found: dict[str, OfficialModelLink] = {}
    for href, label in parser.links:
        url = urllib.parse.urljoin(product_url, href)
        if _outside_vendor_reason(url, origins):
            continue
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        words = f"{label} {url}".lower()
        native = "ltspice" in words
        if "encrypted" in words and "unencrypted" not in words:
            continue
        if suffix in _FORBIDDEN_SUFFIXES or suffix in {".html", ".htm", ".pdf"}:
            continue
        if (
            native
            or ("spice" in words and (suffix == ".zip" or "/zip/" in url))
            or suffix in _MODEL_SUFFIXES - {".txt"}
        ):
            found.setdefault(url, OfficialModelLink(url, label, native))
    return tuple(
        sorted(
            found.values(),
            key=lambda link: (not link.native_ltspice, "tina" in (link.label + link.url).lower()),
        )
    )


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\0" in data:
        raise OfficialSpiceError("model_not_plaintext: binary or encrypted model unsupported")
    return data.decode("utf-8-sig", errors="replace")


def _archive_members(data: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_FILES or sum(info.file_size for info in infos) > _MAX_EXPANDED:
            raise OfficialSpiceError("archive_limit: too many files or expanded size")
        case_paths: set[str] = set()
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or any(":" in part for part in path.parts)
                or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
                or info.flag_bits & 1
            ):
                raise OfficialSpiceError("archive_path_refused: unsafe path, link or encryption")
            if info.is_dir():
                continue
            relative = path.as_posix()
            if relative.casefold() in case_paths:
                raise OfficialSpiceError("archive_duplicate_path: ambiguous file name")
            case_paths.add(relative.casefold())
            if info.file_size > _MAX_MEMBER or path.suffix.lower() in _FORBIDDEN_SUFFIXES:
                raise OfficialSpiceError("archive_member_refused: executable or excessive size")
            if any(part.rstrip(". ") != part or safe_filename(part) != part for part in path.parts):
                raise OfficialSpiceError("archive_path_refused: unsafe Windows filename")
            members[relative] = archive.read(info)
    return members


def _inspect(members: dict[str, bytes], root: Path) -> tuple[OfficialSpiceEntry, ...]:
    entries: list[OfficialSpiceEntry] = []
    known = {name.casefold(): name for name in members}
    pending = [name for name in members if Path(name).suffix.lower() in _MODEL_SUFFIXES]
    inspected: set[str] = set()
    while pending:
        relative = pending.pop(0)
        if relative in inspected:
            continue
        inspected.add(relative)
        text = _decode(members[relative])
        if _ENCRYPTED.search(text):
            raise OfficialSpiceError("encrypted_model_unsupported: original cannot be inspected")
        logicals = list(_logical_lines(text))
        if any(line.garbage for line in logicals):
            raise OfficialSpiceError("model_text_refused: control characters or excessive line")
        for line in logicals:
            tokens = _tokens(line.text)
            if not tokens:
                continue
            head = tokens[0].value.lower()
            if head.startswith("!") or (head.startswith(".") and head not in _DIRECTIVES):
                raise OfficialSpiceError("external_execution_refused: unsupported model directive")
            if re.search(r"(?i)\b(?:file|wavefile)\s*=", line.text):
                raise OfficialSpiceError(
                    "external_file_refused: unsupported element file reference"
                )
            if head not in (".include", ".inc", ".lib"):
                continue
            if len(tokens) < 2:
                raise OfficialSpiceError("dependency_missing: include has no target")
            target = tokens[1].value
            if (
                head == ".lib"
                and len(tokens) == 2
                and re.fullmatch(r"[A-Za-z0-9_+-]+", target)
                and not tokens[1].quoted
                and any(
                    re.fullmatch(r"(?i)\s*\.endl\s+" + re.escape(target) + r"\s*", other.text)
                    for other in logicals
                )
            ):
                continue
            # Text-level rules precede resolve: a Windows UNC resolve can contact a server.
            normalized = target.replace("\\", "/")
            if (
                normalized.startswith("/")
                or ":" in normalized
                or ".." in normalized.split("/")
                or re.search(r"%[^%\s]*%|\$\{|\$[A-Za-z_]", target)
            ):
                raise OfficialSpiceError("dependency_outside_bundle: unsafe include target")
            candidate = (root / relative).parent / normalized
            try:
                resolved = candidate.resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError) as exc:
                raise OfficialSpiceError(
                    "dependency_outside_bundle: absolute or escaping include"
                ) from exc
            if resolved.casefold() not in known:
                raise OfficialSpiceError("dependency_missing: included file not in original bundle")
            pending.append(known[resolved.casefold()])
        for line in logicals:
            tokens = _tokens(line.text)
            if len(tokens) < 2 or tokens[0].value.lower() not in (".subckt", ".model"):
                continue
            name = tokens[1].value
            if not _SAFE_TOKEN.fullmatch(name):
                raise OfficialSpiceError("entry_name_refused: unsafe model identifier")
            if tokens[0].value.lower() == ".subckt":
                ports: list[str] = []
                for index, token in enumerate(tokens[2:], start=2):
                    value = token.value
                    if (
                        value.lower() == "params:"
                        or "=" in value
                        or (index + 1 < len(tokens) and tokens[index + 1].value.startswith("="))
                    ):
                        break
                    if not _SAFE_TOKEN.fullmatch(value):
                        raise OfficialSpiceError("entry_ports_refused: ambiguous model port token")
                    ports.append(value)
                if not ports or len(ports) > 128:
                    raise OfficialSpiceError("entry_ports_refused: empty or excessive model ports")
                entries.append(OfficialSpiceEntry("subckt", name, tuple(ports), root / relative))
            elif len(tokens) >= 3:
                model_type = tokens[2].value.split("(", 1)[0].upper()
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", model_type):
                    raise OfficialSpiceError("entry_model_type_refused: invalid model type")
                entries.append(OfficialSpiceEntry("model", name, (), root / relative, model_type))
    if not entries:
        raise OfficialSpiceError("spice_entry_missing: no inspectable .SUBCKT or .MODEL")
    return tuple(entries)


def _refuse_storage_links(path: Path) -> None:
    # An exists/lstat on a child may follow a parent junction. Check from the
    # filesystem root down, before any child resolve, read or write.
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise OfficialSpiceError("original_path_link: refusing redirected evidence storage")


def acquire_official_spice(
    url: str, out_dir: Path | str, *, allowed_hosts: Sequence[str], timeout_s: float = 20
) -> OfficialSpiceBundle:
    """Save exact original model bytes and dependencies; no simulation or adaptation occurs."""
    output_text = str(out_dir).replace("\\", "/")
    if output_text.startswith("//") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", output_text):
        raise OfficialSpiceError("original_path_refused: network or device output path unsupported")
    data, media_type, final_url = _download(
        url,
        allowed_hosts=allowed_hosts,
        timeout_s=timeout_s,
        max_bytes=_MAX_DOWNLOAD,
        accepted_types=_TYPES,
    )
    digest = hashlib.sha256(data).hexdigest()
    selected = Path(out_dir).absolute()
    _refuse_storage_links(selected)
    root = selected.resolve() / "official" / digest / "original"
    is_archive = zipfile.is_zipfile(BytesIO(data))
    if is_archive:
        try:
            members = _archive_members(data)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise OfficialSpiceError("archive_invalid: model package cannot be inspected") from exc
    else:
        if len(data) > _MAX_MEMBER:
            raise OfficialSpiceError("model_too_large: plaintext model exceeds per-file limit")
        name = Path(urllib.parse.urlsplit(final_url).path).name
        if Path(name).suffix.lower() not in _MODEL_SUFFIXES:
            # Some official download endpoints have no suffix. Preserve bytes in a named library.
            name = "original.lib"
        members = {safe_filename(name): data}
    for name in members:
        _refuse_storage_links(root / name)
    _refuse_storage_links(root.parent / "source.zip")
    _refuse_storage_links(root.parent / "acquisition.json")
    entries = _inspect(members, root)
    files = tuple(
        OfficialSpiceFile(name, hashlib.sha256(payload).hexdigest(), len(payload))
        for name, payload in sorted(members.items())
    )
    originals = dict(members)
    if is_archive:
        originals["../source.zip"] = data
    for name, payload in originals.items():
        target = root / name
        _refuse_storage_links(target)
        if target.exists() and target.read_bytes() != payload:
            raise OfficialSpiceError("original_changed: refusing to overwrite original evidence")
    manifest_path = root.parent / "acquisition.json"
    if manifest_path.is_symlink() or manifest_path.is_junction():
        raise OfficialSpiceError("original_path_link: refusing redirected evidence manifest")
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in members.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(payload)
    if is_archive:
        source_package = root.parent / "source.zip"
        if not source_package.exists():
            source_package.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "kind": "official_manufacturer_original",
        "source_url": url,
        "final_url": final_url,
        "source_sha256": digest,
        "content_type": media_type,
        "original_package": "source.zip" if is_archive else None,
        "files": [
            {"path": item.relative_path, "sha256": item.sha256, "size": item.size} for item in files
        ],
        "adapted": False,
        "ltspice_compatibility": "NOT_RUN",
        "electrical_validation": "UNKNOWN",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return OfficialSpiceBundle(url, digest, root, files, entries, final_url)
