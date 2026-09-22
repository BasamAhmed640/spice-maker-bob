"""Internet reinforcement: public sources recorded as claims, never as verdicts.

Honesty rules this module implements:

* the datasheet rows stay the only pass/fail oracle. Nothing here produces a
  status for the model, changes a limit, edits the frozen spec or can fail a
  build: the worst outcome is ``unavailable`` with a reason and the build
  continues;
* everything recorded as fact is text this module retrieved itself.
  ``SourceRecord.retrieved`` is ``True`` only for bytes a fetcher returned,
  ``sha256`` is the digest of exactly those bytes and ``excerpt`` is their
  whitespace-collapsed text. An agent claim about a URL that could not be
  fetched stays ``retrieved=False`` and its ``reason`` starts with
  ``unverified_claim:``;
* ``caveats`` quote sentences from retrieved excerpts only, each labelled with
  its source URL; ``suggested_probes`` name probe ids from
  :data:`boardmodeler.authoring.probes.PROBES` whose vocabulary occurs in
  retrieved text. Both are suggestions for a human, never a change to the spec;
* no raw response body is written anywhere except ``spec/supporting.json``
  (the excerpts inside it).

The default fetcher is :func:`default_fetcher`: urllib with an explicit
User-Agent, Python's default TLS verification, a public-host check (loopback,
private, link-local, multicast and reserved destinations are refused, and a
hostname that resolves to one of those or does not resolve at all is refused),
a 1 MiB size cap, a content-type allowlist (``text/html``, ``text/plain``,
``application/pdf``) and at most three redirects, each re-checked for a public
host. A refusal, timeout, HTTP error or unreadable body is recorded on the
source as a ``reason``; nothing raises out of :func:`reinforce`.

When no ``candidate_provider`` is injected, the stage asks the build's own agent
backend (the one the author loop is using, or the configured provider when none
was injected) for candidate URLs, through :func:`query_agent_backend`. The prompt
forbids guessing URLs and demands a strict JSON reply; any reply that is not
exactly the requested shape yields zero candidates. This module never invents a
URL and never hard-codes a vendor URL.

Report file (``<out_dir>/spec/supporting.json``, UTF-8, LF, indent 2, keys
sorted)::

    {
      "caveats": ["<url>: <verbatim sentence>", ...],
      "detail": "<machine_readable_prefix>: <what happened>",
      "enabled": true,
      "part": "TPS54320",
      "sources": [
        {
          "claim": "<one line the agent gave for this URL>",
          "content_type": "text/html",
          "excerpt": "<retrieved text, collapsed, <= 4000 chars>",
          "reason": null,
          "retrieved": true,
          "retrieved_utc": "2026-09-18T12:00:00Z",
          "sha256": "<sha256 of the retrieved bytes>",
          "url": "https://..."
        },
        {
          "claim": "...",
          "content_type": null,
          "excerpt": null,
          "reason": "unverified_claim: http_error: HTTP 404 Not Found",
          "retrieved": false,
          "retrieved_utc": null,
          "sha256": null,
          "url": "https://..."
        }
      ],
      "spec_digest": "<digest of the frozen spec this run belongs to>",
      "status": "ok",
      "suggested_probes": ["uvlo_rise", ...]
    }
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pypdf import PdfReader

from boardmodeler.agent_providers import CATALOG
from boardmodeler.authoring.probes import PROBES
from boardmodeler.domain.hashing import sha256_bytes
from boardmodeler.domain.records import DocumentRecord

if TYPE_CHECKING:  # the backends module imports nothing from here, but keep it lazy
    from boardmodeler.authoring.backends import AuthorBackend

__all__ = [
    "FetchRefused",
    "ReinforcementReport",
    "SourceRecord",
    "build_candidate_prompt",
    "default_fetcher",
    "parse_agent_reply",
    "query_agent_backend",
    "reinforce",
    "vendor_hosts",
]

#: Where the report lives inside the build's ``out_dir``; the frozen
#: specification lives in the same directory (``spec/characteristics.json``).
_SPEC_DIR = "spec"
_SUPPORTING_FILENAME = "supporting.json"

_DEFAULT_TIMEOUT_S = 30.0

#: Excerpt cap: documented in the report docstring above.
_MAX_EXCERPT_CHARS = 4000
#: "Too large" is a refusal of the whole body, never a silent truncation.
_MAX_RESPONSE_BYTES = 1 << 20
#: urllib refuses the fourth hop; the observed reason says so.
_MAX_REDIRECTS = 3
_USER_AGENT = "BoardModeler/1.0 (internet-reinforcement stage)"
_ACCEPT_HEADER = "text/html, text/plain, application/pdf"
_ACCEPTED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/pdf"})

#: A PDF response is read through pypdf, bounded so a pathological file cannot
#: make the stage expensive.
_MAX_PDF_PAGES = 50
_MAX_PDF_CHARS = 20_000

_MAX_CAVEATS = 12
_MAX_CAVEATS_PER_SOURCE = 3
_MAX_CAVEAT_CHARS = 400

_MAX_JSON_OBJECTS = 200
_MAX_JSON_NESTING = 2

#: A vendor's own web host is often published with one of these labels in front
#: of the registrable domain; the label says nothing about ownership, so it is
#: dropped before a host is stored or compared.
_WWW_PREFIXES = ("www.", "ww1.", "www1.")
#: How many allowed hosts a refusal reason names before it is abbreviated.
_MAX_HOSTS_IN_REASON = 6

_HTTP_URL_SCHEMES = ("http", "https")

#: Markers that make a *retrieved* sentence worth quoting as a caveat.
_CAVEAT_MARKERS = (
    "errata",
    "erratum",
    "workaround",
    "known issue",
    "caveat",
    "limitation",
    "not recommended",
    "unsupported",
    "not supported",
    "does not support",
    "incompatible",
    "not compatible",
    "cannot be used",
    "must not exceed",
    "advisory",
    "affected revision",
    "device revision",
    "silicon revision",
)

#: Which probe questions a retrieved sentence suggests. Matching is plain
#: substring matching on the collapsed lowercase excerpt; a probe is suggested
#: when any of its keywords occurs. Keys must exist in ``PROBES`` (checked at
#: use, so a registry rename degrades to a missing suggestion, not an error).
_PROBE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "uvlo_rise": ("uvlo", "undervoltage lockout", "under-voltage lockout", "turn-on threshold"),
    "uvlo_fall": ("uvlo hysteresis", "uvlo falling", "undervoltage hysteresis"),
    "en_rise": ("enable threshold", "enable pin", "en threshold", "enable rising"),
    "en_fall": ("enable falling", "disable threshold", "enable hysteresis", "en hysteresis"),
    "vref": ("reference voltage", "feedback voltage", "vref", "feedback regulation"),
    "load_regulation": ("load regulation", "load transient", "line regulation", "dc accuracy"),
    "current_limit": (
        "current limit",
        "overcurrent",
        "over-current",
        "peak current",
        "hiccup",
    ),
    "pg_threshold": (
        "power good",
        "power-good",
        "pgood",
        "pg pin",
        "power-good threshold",
    ),
    "soft_start": ("soft start", "soft-start", "softstart", "ss pin", "startup ramp"),
    "shutdown_current": ("shutdown current", "standby current", "disabled current"),
    "quiescent_current": ("quiescent current", "supply current", "no-load current"),
}


class FetchRefused(RuntimeError):
    """A retrieval refused by policy: scheme, size cap or content type.

    ``str(exc)`` is already a machine-readable reason (``scheme_refused: ...``);
    :func:`reinforce` records it on the source instead of raising.
    """


@dataclass(frozen=True)
class SourceRecord:
    """One candidate source and exactly how far the stage got with it.

    ``retrieved=True`` means the bytes below were fetched by this process:
    ``sha256`` digests them and ``excerpt`` is their text. Everything else stays
    an unverified claim. ``excerpt`` is ``None`` when nothing was retrieved and
    also when retrieved bytes carried no readable text (a PDF without a text
    layer); ``reason`` then says what limited the reading. ``retrieved_utc`` is
    when exactly these bytes were first retrieved for this project; a re-run
    that fetches the same bytes keeps the original stamp so the report stays
    byte-stable.
    """

    url: str
    claim: str
    retrieved: bool
    excerpt: str | None
    sha256: str | None
    content_type: str | None
    retrieved_utc: str | None
    reason: str | None


@dataclass(frozen=True)
class ReinforcementReport:
    """The stage's whole output; ``status`` is ``ok``/``skipped``/``unavailable``."""

    part: str
    enabled: bool
    status: str
    detail: str
    sources: tuple[SourceRecord, ...]
    caveats: tuple[str, ...]
    suggested_probes: tuple[str, ...]
    spec_digest: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "spec_digest": self.spec_digest,
            "enabled": self.enabled,
            "status": self.status,
            "detail": self.detail,
            "sources": [_source_payload(source) for source in self.sources],
            "caveats": list(self.caveats),
            "suggested_probes": list(self.suggested_probes),
        }

    def to_json(self) -> str:
        """Stable JSON text: sorted keys, indent 2, trailing newline, LF only."""
        return json.dumps(self.payload(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ReinforcementReport:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"report is not valid JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ValueError(f"report must be a JSON object, got {type(data).__name__}")
        return cls(
            part=str(data["part"]),
            enabled=bool(data["enabled"]),
            status=str(data["status"]),
            detail=str(data["detail"]),
            sources=tuple(_source_from_payload(entry) for entry in data.get("sources", ())),
            caveats=tuple(str(item) for item in data.get("caveats", ())),
            suggested_probes=tuple(str(item) for item in data.get("suggested_probes", ())),
            spec_digest=str(data.get("spec_digest", "")),
        )


def _source_payload(source: SourceRecord) -> dict[str, Any]:
    return {
        "url": source.url,
        "claim": source.claim,
        "retrieved": source.retrieved,
        "excerpt": source.excerpt,
        "sha256": source.sha256,
        "content_type": source.content_type,
        "retrieved_utc": source.retrieved_utc,
        "reason": source.reason,
    }


def _source_from_payload(entry: object) -> SourceRecord:
    if not isinstance(entry, Mapping):
        raise ValueError(f"source entry must be a JSON object, got {type(entry).__name__}")

    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("source entry has no string 'url'")

    def text(key: str) -> str | None:
        value = entry.get(key)
        return None if value is None else str(value)

    return SourceRecord(
        url=url,
        claim=str(entry.get("claim", "")),
        retrieved=bool(entry.get("retrieved", False)),
        excerpt=text("excerpt"),
        sha256=text("sha256"),
        content_type=text("content_type"),
        retrieved_utc=text("retrieved_utc"),
        reason=text("reason"),
    )


def _utc_now() -> str:
    """Retrieval timestamp, ISO 8601 UTC with a ``Z`` suffix.

    A module-level function so tests (and callers that need a byte-stable
    report) can freeze it; the default is the wall clock in UTC.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# fetching


def _normalize_host(value: str | None) -> str | None:
    """A comparable host: lowercase, no port, no trailing root dot, no ``www.`` label.

    Accepts a URL or a bare host. ``None`` means "no host to compare", which a
    caller must treat as a refusal, never as a wildcard.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = urllib.parse.urlsplit(text)
    if parts.hostname is None:  # a bare ``example.com/path`` never has a scheme
        parts = urllib.parse.urlsplit(f"//{text}")
    host = parts.hostname
    if host is None:
        return None
    host = host.strip().strip(".").lower()
    for prefix in _WWW_PREFIXES:
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix) :]
            break
    return host or None


def _host_belongs_to(host: str, vendor_host: str) -> bool:
    """Whether ``host`` is the vendor host or a subdomain of it.

    Suffix matching demands the label boundary: ``evil-ti.com`` and
    ``www.ti.com.evil.test`` are both outside ``ti.com``, while ``e2e.ti.com``
    is a host the vendor's own DNS controls.
    """
    return host == vendor_host or host.endswith(f".{vendor_host}")


def _host_list(hosts: Sequence[str]) -> str:
    if not hosts:
        return "none"
    shown = list(hosts[:_MAX_HOSTS_IN_REASON])
    if len(hosts) > len(shown):
        shown.append(f"+{len(hosts) - len(shown)} more")
    return ", ".join(shown)


def _document_source_urls(out_dir: Path) -> Iterator[str]:
    """``source_url`` of every readable ``docs/*.json`` :class:`DocumentRecord`.

    A record that cannot be read or validated contributes no host; that is a
    narrower allowlist, never a wider one, so the stage does not raise here.
    """
    for path in sorted((out_dir / "docs").glob("*.json")):
        try:
            record = DocumentRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if record.source_url:
            yield record.source_url


def _vendor_io_source_urls(out_dir: Path) -> Iterator[str]:
    """``source_url`` of every readable ``vendor-io/*/manifest.json`` attribution."""
    for path in sorted((out_dir / "vendor-io").glob("*/manifest.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        url = payload.get("source_url") if isinstance(payload, Mapping) else None
        if isinstance(url, str) and url:
            yield url


def vendor_hosts(out_dir: Path | str, *, extra: Sequence[str] = ()) -> tuple[str, ...]:
    """The hosts the reinforcement stage may fetch from, in stable order.

    Derived from evidence the project already recorded, never guessed:

    * the ``source_url`` of every document record in ``out_dir``/``docs`` (the
      datasheet's own provenance) and of every ``vendor-io`` attribution
      manifest, plus any ``extra`` URL a caller already knows;
    * the documented hosts of this build's :data:`CATALOG` entries (their
      documentation and, when present, endpoint URLs), so a vendor whose own
      documentation is shipped with the application stays reachable.

    A host matches itself and its subdomains; nothing else. The set is never
    empty in a shipped build, because every catalog entry carries documentation.
    """
    hosts: list[str] = []
    root = Path(out_dir)
    urls: list[str] = [*extra, *_document_source_urls(root), *_vendor_io_source_urls(root)]
    for entry in CATALOG:
        urls.extend(url for url in (entry.docs, entry.endpoint) if url)
    for url in urls:
        host = _normalize_host(url)
        if host is not None and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def _outside_vendor_reason(url: str, allowed_hosts: Sequence[str]) -> str | None:
    """``None`` when ``url`` belongs to the vendor, else the machine-readable reason."""
    host = _normalize_host(url)
    if host is None:
        return f"vendor_refused: {_safe_url(url)} names no host to match against the vendor"
    if any(_host_belongs_to(host, vendor_host) for vendor_host in allowed_hosts):
        return None
    return (
        f"vendor_refused: host {host!r} is outside the part vendor's sites "
        f"(allowed: {_host_list(allowed_hosts)})"
    )


def _is_public_address(address: str) -> bool:
    """True only for a globally routable address (no private/reserved/link-local)."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    target = mapped if mapped is not None else parsed
    if (
        target.is_private
        or target.is_loopback
        or target.is_link_local
        or target.is_multicast
        or target.is_reserved
        or target.is_unspecified
    ):
        return False
    return target.is_global


def _require_public_host(url: str) -> None:
    """Refuse a destination that is not a public internet host.

    A literal address is classified directly; a hostname must resolve, and every
    address it resolves to must be globally routable. A host that does not
    resolve is refused rather than attempted. The redirect handler calls this
    for each hop so a redirect cannot escape the policy.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise FetchRefused(f"host_refused: {url!r} has no host")
    addresses: list[str] | None = None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        addresses = [info[4][0] for info in infos]
    if addresses is None:
        addresses = [host]
    if not addresses:
        raise FetchRefused(f"host_refused: {host!r} does not resolve")
    for address in addresses:
        if not _is_public_address(address):
            raise FetchRefused(f"host_refused: {host!r} resolves to non-public address {address}")


class _RedirectCap(HTTPRedirectHandler):
    """At most :data:`_MAX_REDIRECTS` hops, each still a public host."""

    max_repeats = _MAX_REDIRECTS
    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _require_public_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetcher(url: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> tuple[bytes, str]:
    """Retrieve one public URL: size cap, content-type allowlist, 3 redirects.

    TLS verification stays at Python's default (no custom context is passed).
    Raises :class:`FetchRefused` for a policy refusal and lets urllib's
    ``HTTPError``/``URLError`` and ``TimeoutError`` travel; :func:`reinforce`
    turns any of them into a recorded ``reason``.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in _HTTP_URL_SCHEMES or not parts.netloc:
        raise FetchRefused(f"scheme_refused: {url!r} is not an http(s) URL")
    if parts.username or parts.password:
        raise FetchRefused("credentials_in_url: refusing a URL that carries a secret")
    if timeout_s <= 0:
        raise FetchRefused(f"invalid_timeout: timeout_s must be > 0, got {timeout_s!r}")
    _require_public_host(url)
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT_HEADER})
    opener = build_opener(_RedirectCap())
    with opener.open(request, timeout=timeout_s) as response:
        content_type = response.headers.get_content_type()
        if content_type not in _ACCEPTED_CONTENT_TYPES:
            raise FetchRefused(
                f"content_type_refused: {content_type!r} (accepted: "
                "text/html, text/plain, application/pdf)"
            )
        declared = _int_or_none(response.headers.get("Content-Length"))
        if declared is not None and declared > _MAX_RESPONSE_BYTES:
            raise FetchRefused(
                f"too_large: Content-Length {declared} exceeds {_MAX_RESPONSE_BYTES} bytes"
            )
        data = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise FetchRefused(f"too_large: response exceeds {_MAX_RESPONSE_BYTES} bytes")
    return data, content_type


def _int_or_none(value: object) -> int | None:
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _reason_for(exc: BaseException) -> str:
    """A machine-readable reason for one failed retrieval; never a secret."""
    if isinstance(exc, FetchRefused):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if 300 <= exc.code < 400:
            return f"redirect_limit: stopped after {_MAX_REDIRECTS} redirects (HTTP {exc.code})"
        return f"http_error: HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"url_error: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return f"timeout: {exc}"
    if isinstance(exc, ValueError):
        return f"invalid_url: {exc}"
    return f"fetch_error: {type(exc).__name__}: {exc}"


def _fetch(
    url: str, fetcher: Callable[[str], tuple[bytes, str]]
) -> tuple[bytes | None, str | None, str | None]:
    """``(data, media_type, failure)`` for one URL; a fetcher can never raise out."""
    try:
        result = fetcher(url)
    except Exception as exc:
        return None, None, _reason_for(exc)
    if not isinstance(result, (tuple, list)) or len(result) != 2:
        return (
            None,
            None,
            (
                "fetcher_contract: expected a (bytes, content_type) pair, got "
                f"{type(result).__name__}"
            ),
        )
    data, content_type = result
    if not isinstance(data, bytes):
        return None, None, f"fetcher_contract: payload is {type(data).__name__}, expected bytes"
    media_type = _media_type(content_type)
    if media_type not in _ACCEPTED_CONTENT_TYPES:
        return (
            None,
            None,
            (
                f"content_type_refused: {media_type!r} (accepted: "
                "text/html, text/plain, application/pdf)"
            ),
        )
    return data, media_type, None


def _media_type(value: object) -> str:
    if isinstance(value, str):
        return value.split(";", 1)[0].strip().lower()
    return ""


# --------------------------------------------------------------------------- #
# text


def _collapse(text: str) -> str:
    """Whitespace-collapsed text: the verbatim characters, one-line."""
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text) if part]


def _pdf_text(data: bytes) -> tuple[str | None, str | None]:
    """``(text, problem)`` from a PDF's text layer, bounded and never raising."""
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        return None, f"pdf_unreadable: {type(exc).__name__}: {exc}"
    chunks: list[str] = []
    total = 0
    try:
        pages = min(len(reader.pages), _MAX_PDF_PAGES)
        for index in range(pages):
            text = reader.pages[index].extract_text() or ""
            chunks.append(text)
            total += len(text)
            if total >= _MAX_PDF_CHARS:
                break
    except Exception as exc:
        return None, f"pdf_unreadable: {type(exc).__name__}: {exc}"
    return "\n".join(chunks), None


def _excerpt_of(data: bytes, content_type: str) -> tuple[str | None, str | None]:
    """``(excerpt, limitation)`` for an accepted content type."""
    if content_type == "application/pdf":
        text, problem = _pdf_text(data)
        if problem is not None:
            return None, problem
    elif content_type in ("text/html", "text/plain"):
        text = data.decode("utf-8", errors="replace")
    else:  # unreachable through _fetch; kept so an unknown type is never decoded as fact
        return None, f"text_unavailable: no reader for {content_type!r}"
    collapsed = _collapse(text)
    if not collapsed:
        return None, "empty_text: the retrieved body carries no readable text"
    return collapsed[:_MAX_EXCERPT_CHARS], None


# --------------------------------------------------------------------------- #
# candidates


def _is_http_url(url: str) -> bool:
    if not url or any(char.isspace() for char in url):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in _HTTP_URL_SCHEMES or not parts.netloc:
        return False
    return not parts.username and not parts.password


def _safe_url(url: str) -> str:
    """A URL safe to put in a reason: no credentials, no query string."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "<unparsable-url>"
    if not parts.scheme or not parts.netloc:
        return "<unparsable-url>"
    host = parts.hostname or ""
    if port:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _clean_candidates(
    raw: object, max_sources: int, *, origin: str
) -> tuple[list[tuple[str, str]], str]:
    """``(candidates, note)``; a partly malformed list yields no candidates at all.

    A reply is trusted only when every entry is exactly the requested shape
    (objects with a non-empty http(s) ``url`` and a non-empty ``claim``).
    """
    if not isinstance(raw, (list, tuple)):
        return [], f"{origin}_malformed: expected a list of sources, got {type(raw).__name__}"
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            return [], (
                f"{origin}_malformed: source entry is {type(entry).__name__}, expected an object"
            )
        url = entry.get("url")
        claim = entry.get("claim")
        if not isinstance(url, str) or not isinstance(claim, str):
            return [], f"{origin}_malformed: every source entry needs string 'url' and 'claim'"
        url = url.strip()
        claim = _collapse(claim)
        if not _is_http_url(url) or not claim:
            return [], f"{origin}_malformed: rejected source entry {_safe_url(url)!r}"
        if url in seen:
            continue
        seen.add(url)
        candidates.append((url, claim))
        if len(candidates) >= max_sources:
            break
    return candidates, ""


def _reply_candidates(text: str, max_sources: int) -> tuple[list[tuple[str, str]], str]:
    """``(candidates, note)`` from raw agent text; malformed text is no candidates."""
    if not isinstance(text, str) or not text.strip():
        return [], "candidate_reply_empty: the agent backend returned no text"
    for payload in _json_objects(text):
        if "sources" not in payload:
            continue
        candidates, note = _clean_candidates(
            payload["sources"], max_sources, origin="candidate_reply"
        )
        if note:
            return [], note
        if not candidates:
            return [], "candidate_reply_empty: the agent backend listed no sources"
        return candidates, ""
    return [], "candidate_reply_unparsed: no JSON object with a 'sources' list was found"


def parse_agent_reply(text: str, max_sources: int) -> tuple[tuple[str, str], ...]:
    """Strictly parse an agent reply into ``(url, claim)`` candidates.

    The reply must carry one JSON object with a ``sources`` list whose entries
    are objects with a non-empty ``url`` and ``claim``. Anything else — invalid
    JSON, a different shape, a non-http URL, an entry missing a field — yields
    no candidates: a partly usable answer is not trusted to be partly correct.
    A JSON reply nested as a string value (an agent harness wrapping the answer)
    is recognised up to a small depth.
    """
    if max_sources < 1:
        return ()
    candidates, _note = _reply_candidates(text, max_sources)
    return tuple(candidates)


def _json_objects(text: str, *, depth: int = 0) -> Iterator[dict[str, Any]]:
    for payload in _scan_objects(text):
        yield payload
        if depth < _MAX_JSON_NESTING:
            for value in payload.values():
                if isinstance(value, str) and "{" in value:
                    yield from _json_objects(value, depth=depth + 1)


def _scan_objects(text: str) -> Iterator[dict[str, Any]]:
    """Every JSON object in ``text``: whole-text parse first, then scanning."""
    decoder = json.JSONDecoder()
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            whole = json.loads(stripped)
        except ValueError:
            whole = None
        if isinstance(whole, Mapping):
            yield dict(whole)
    found = 0
    index = text.find("{")
    while index >= 0 and found < _MAX_JSON_OBJECTS:
        found += 1
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, Mapping):
            yield dict(value)
        index = text.find("{", end)


def build_candidate_prompt(part: str, max_sources: int, vendor_hosts: Sequence[str] = ()) -> str:
    """The read-only question the agent backend is asked for candidate URLs.

    ``vendor_hosts`` names the sites the stage is allowed to fetch from, so the
    asking turn already stays on the part vendor instead of proposing URLs the
    policy will refuse.
    """
    shape = (
        '{"sources": [{"url": "https://...", "claim": "one line: what this source '
        'shows about the part"}]}'
    )
    vendor_rule = (
        "\nOnly name URLs on the part vendor's own sites ("
        f"{_host_list(vendor_hosts)}); the search may not leave the vendor."
        if vendor_hosts
        else ""
    )
    return (
        f"List up to {max_sources} public sources a SPICE model author should read about the "
        f"exact part number {part!r}: errata and silicon advisories, application notes, "
        "thermal and layout guidance, vendor SPICE-model release notes and their known caveats.\n"
        "Answer with one JSON object and nothing else, in exactly this shape:\n"
        f"{shape}\n"
        "Rules: only URLs you are confident exist; never guess or construct a URL from a "
        "pattern; one short line per claim; do not repeat a URL; no prose outside the JSON "
        f"object.{vendor_rule}"
    )


def query_agent_backend(
    prompt: str,
    workdir: Path,
    *,
    backend: AuthorBackend | None = None,
    cancel: threading.Event | None = None,
    timeout_s: float | None = None,
) -> tuple[str, str]:
    """One read-only turn of the agent backend: ``(reply, note)``.

    ``backend`` is the build's own backend, so the search runs on the same agent
    the author loop uses; without one the configured provider is built through
    :func:`~boardmodeler.authoring.api_backend.build_api_backend` (the catalog's
    default provider when nothing is configured). The turn is a text turn
    (``expect_text``): it must not write a file, and the answer is returned as it
    was given.

    ``note`` is empty when the backend produced text, otherwise it names why no
    turn could be made (``agent_backend_unavailable: ...``). ``cancel`` is the
    build's cancellation event, threaded to the backend so a CANCEL stops a hung
    agent; ``timeout_s`` bounds this one invocation when the search has a budget
    (a default is used when the budget is unbounded, because one agent turn
    still needs an upper bound). The credential is handled by the backend itself
    and never appears here. Never raises; a callable seam so tests can answer
    without any agent, network or credential.
    """
    try:
        from boardmodeler.authoring.api_backend import DEFAULT_TIMEOUT_S
        from boardmodeler.authoring.backends import AuthorRequest

        limit = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
        chosen = backend
        if chosen is None:
            from boardmodeler.authoring.api_backend import build_api_backend

            chosen = build_api_backend(timeout_s=limit)
        usable, reason = chosen.availability()
        if not usable:
            return "", f"agent_backend_unavailable: {reason}"
        request = AuthorRequest(
            prompt=prompt,
            workdir=Path(workdir),
            model_dir=Path(workdir),
            max_turns=1,
            expect_text=True,
        )
        result = chosen.author(request, cancel, timeout_s=limit)
        if not result.ok:
            return "", f"agent_backend_failed: {result.detail}"
        if not result.stdout_tail.strip():
            return "", "candidate_reply_empty: the agent backend returned no text"
        return result.stdout_tail, ""
    except Exception as exc:
        return "", f"agent_backend_error: {type(exc).__name__}: {exc}"


def _provider_candidates(
    provider: Callable[[str], Sequence[tuple[str, str]]], part: str, max_sources: int
) -> tuple[list[tuple[str, str]], str]:
    """``(candidates, note)`` from an injected provider; it may never raise out."""
    try:
        raw = provider(part)
    except Exception as exc:
        return [], f"candidate_provider_error: {type(exc).__name__}: {exc}"
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return [], (
            "candidate_provider_malformed: expected a sequence of (url, claim) pairs, got "
            f"{type(raw).__name__}"
        )
    entries: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            entries.append({"url": item[0], "claim": item[1]})
        else:
            return [], (
                "candidate_provider_malformed: every candidate must be a (url, claim) pair, got "
                f"{type(item).__name__}"
            )
    return _clean_candidates(entries, max_sources, origin="candidate_provider")


# --------------------------------------------------------------------------- #
# what retrieved text supports


def _quote(sentence: str) -> str:
    """A verbatim sentence, with a trailing ellipsis only when it was cut."""
    if len(sentence) <= _MAX_CAVEAT_CHARS:
        return sentence
    return sentence[: _MAX_CAVEAT_CHARS - 1].rstrip() + "\u2026"


def _caveats(records: Sequence[SourceRecord]) -> tuple[str, ...]:
    """Sentences from retrieved excerpts that name a limitation, URL first."""
    caveats: list[str] = []
    for record in records:
        if not record.retrieved or not record.excerpt:
            continue
        per_source = 0
        for sentence in _sentences(record.excerpt):
            lowered = sentence.lower()
            if not any(marker in lowered for marker in _CAVEAT_MARKERS):
                continue
            caveats.append(f"{record.url}: {_quote(sentence)}")
            per_source += 1
            if per_source >= _MAX_CAVEATS_PER_SOURCE or len(caveats) >= _MAX_CAVEATS:
                break
        if len(caveats) >= _MAX_CAVEATS:
            break
    return tuple(caveats)


def _suggested_probes(records: Sequence[SourceRecord]) -> tuple[str, ...]:
    """Probe ids whose vocabulary occurs in retrieved text, in registry order."""
    haystack = "\n".join(
        record.excerpt for record in records if record.retrieved and record.excerpt
    ).lower()
    if not haystack:
        return ()
    suggestions: list[str] = []
    for probe_id in PROBES:
        keywords = _PROBE_KEYWORDS.get(probe_id)
        if keywords and any(keyword in haystack for keyword in keywords):
            suggestions.append(probe_id)
    return tuple(suggestions)


# --------------------------------------------------------------------------- #
# the stage


def _prior_retrievals(out_dir: Path) -> dict[str, SourceRecord]:
    """``url -> record`` from an earlier report in ``out_dir``, when readable.

    Used only so that a re-run whose upstream bytes are unchanged produces the
    same report bytes: the same URL with the same SHA-256 reuses the earlier
    reading and retrieval stamp. A missing or unreadable file is simply no
    prior knowledge.
    """
    path = out_dir / _SPEC_DIR / _SUPPORTING_FILENAME
    try:
        previous = ReinforcementReport.from_json(path.read_text(encoding="utf-8"))
    except OSError, ValueError, KeyError, TypeError:
        return {}
    return {record.url: record for record in previous.sources if record.retrieved and record.sha256}


def _write_report(out_dir: Path, text: str) -> Path:
    target = out_dir / _SPEC_DIR / _SUPPORTING_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def _seconds(value: object, name: str) -> float:
    """``value`` as a number of seconds, or a ``ValueError`` naming the argument."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of seconds, got {value!r}") from exc


MIN_AGENT_TURN_S = 90.0
"""The least budget a supporting-material search is worth starting.

The search's first step is a single agent turn, and a reasoning-class model spends
minutes on one. A smaller budget is therefore *arithmetically* unspendable: the
recorded 1.4.0 build set ``reinforce_timeout_s=45.0``, that turn could not finish
inside it, and the stage burned its entire allowance to return nothing — the log
read ``search_budget_exceeded: the supporting-material search did not finish
within 45 s`` on every build. Below this floor the stage now declines *before*
spending anything, because "give up fast" has to mean giving up before the time is
gone rather than after it.
"""


def reinforce(
    *,
    part: str,
    spec_digest: str,
    out_dir: Path,
    backend: AuthorBackend | None = None,
    candidate_provider: Callable[[str], Sequence[tuple[str, str]]] | None = None,
    fetcher: Callable[[str], tuple[bytes, str]] | None = None,
    enabled: bool = True,
    max_sources: int = 6,
    fetch_timeout_s: float = 30.0,
    timeout_s: float | None = None,
    vendor_urls: Sequence[str] = (),
    cancel: threading.Event | None = None,
) -> ReinforcementReport:
    """Find supporting sources around a model build and record what was retrieved.

    Writes ``<out_dir>/spec/supporting.json`` (indent 2, sorted keys, LF) and
    returns the same report. ``backend`` is the build's own author backend — the
    search then runs on the agent the author loop uses; without it the configured
    provider is built. ``timeout_s`` bounds the whole search (``None``
    leaves it unbounded); when it expires the stage is ``unavailable`` with a
    reason naming the budget and the build continues. ``cancel`` is the build's
    cancellation event, passed to the candidate agent turn.

    The search never leaves the part vendor: a candidate URL is fetched only
    when its host belongs to :func:`vendor_hosts` (the datasheet provenance this
    project recorded, any ``vendor_urls`` the caller already knows, and this
    build's catalog documentation hosts). A candidate outside that set is
    recorded as an ``unverified_claim`` with a ``vendor_refused:`` reason and is
    never fetched; when *no* candidate is inside the set the stage finishes
    immediately as ``no_vendor_source_found`` instead of spending its budget.
    Statuses:

    * ``skipped`` — ``enabled`` is False; zero candidate lookups, zero fetches;
    * ``unavailable`` — enabled, but nothing could be retrieved (no candidates,
      every candidate outside the vendor, every fetch refused, only unreadable
      bodies, or the search budget ran out). Never a failure: the build
      continues, and every reason is on its source record;
    * ``ok`` — at least one candidate's bytes were retrieved and hashed.

    This function never raises for a retrieval, agent or provider problem; only
    a filesystem error while writing the report can propagate.
    """
    out_root = Path(out_dir)
    if (
        enabled
        and candidate_provider is None
        and fetcher is None
        and not (cancel and cancel.is_set())
    ):
        prior = out_root / _SPEC_DIR / _SUPPORTING_FILENAME
        try:
            saved = ReinforcementReport.from_json(prior.read_text(encoding="utf-8"))
            ttl = 3600 if saved.status == "ok" else 600
            if (
                saved.part == part
                and saved.spec_digest == spec_digest
                and saved.enabled
                and time.time() - prior.stat().st_mtime < ttl
            ):
                return saved
        except OSError, ValueError, KeyError, TypeError:
            pass
    budget_s = None if timeout_s is None else _seconds(timeout_s, "timeout_s")
    fetch_budget_s = _seconds(fetch_timeout_s, "fetch_timeout_s")
    deadline = None if budget_s is None else time.monotonic() + budget_s

    def budget_left() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def budget_reason() -> str:
        seconds = 0.0 if budget_s is None else budget_s
        return (
            "search_budget_exceeded: the supporting-material search did not finish within "
            f"{seconds:g} s"
        )

    def finish(
        status: str,
        detail: str,
        sources: Sequence[SourceRecord] = (),
        caveats: Sequence[str] = (),
        probes: Sequence[str] = (),
    ) -> ReinforcementReport:
        report = ReinforcementReport(
            part=part,
            enabled=bool(enabled),
            status=status,
            detail=detail,
            sources=tuple(sources),
            caveats=tuple(caveats),
            suggested_probes=tuple(probes),
            spec_digest=str(spec_digest),
        )
        _write_report(out_root, report.to_json())
        return report

    if not enabled:
        return finish("skipped", "disabled: internet reinforcement was not enabled for this build")
    if max_sources < 1:
        return finish(
            "unavailable",
            f"max_sources_invalid: nothing requested (max_sources={max_sources})",
        )
    if budget_left() == 0.0:
        return finish("unavailable", budget_reason())

    allowed_hosts = vendor_hosts(out_root, extra=vendor_urls)

    if candidate_provider is None:
        # Declining here rather than handing the turn a budget it cannot use is the
        # whole point: the previous behaviour spent the full allowance and then
        # reported the budget as exceeded, which is both slower and less honest than
        # saying up front that the allowance was too small to try.
        left = budget_left()
        if left is not None and left < MIN_AGENT_TURN_S:
            return finish(
                "unavailable",
                f"search_budget_too_small: {left:g} s remained of "
                f"{0.0 if budget_s is None else budget_s:g} s, and one candidate-query turn "
                f"needs about {MIN_AGENT_TURN_S:g} s, so the search was not started; raise "
                "reinforce_timeout_s to enable it",
            )
        with tempfile.TemporaryDirectory(prefix="boardmodeler-reinforce-") as scratch:
            reply, note = query_agent_backend(
                build_candidate_prompt(part, max_sources, allowed_hosts),
                Path(scratch),
                backend=backend,
                cancel=cancel,
                timeout_s=budget_left(),
            )
        candidates = [] if note else None
        if candidates is None:
            candidates, note = _reply_candidates(reply, max_sources)
    else:
        candidates, note = _provider_candidates(candidate_provider, part, max_sources)
        if not candidates and not note:
            note = "candidate_provider_empty: the candidate provider listed no sources"

    if budget_left() == 0.0:
        return finish("unavailable", budget_reason())
    if not candidates:
        return finish("unavailable", note or "no_candidates: nothing to retrieve")

    now = _utc_now()
    prior = _prior_retrievals(out_root)
    records: list[SourceRecord] = []
    attempted = 0
    refused_urls: list[str] = []
    for url, claim in candidates:
        refusal = _outside_vendor_reason(url, allowed_hosts)
        if refusal is not None:
            # A destination outside the vendor is refused here, before any socket and
            # without retry; the record keeps the claim visible as unverified.
            refused_urls.append(url)
            records.append(
                SourceRecord(
                    url=url,
                    claim=claim,
                    retrieved=False,
                    excerpt=None,
                    sha256=None,
                    content_type=None,
                    retrieved_utc=None,
                    reason=f"unverified_claim: {refusal}",
                )
            )
            continue
        left = budget_left()
        if left == 0.0:
            return finish("unavailable", budget_reason())
        attempted += 1
        if fetcher is not None:
            fetch: Callable[[str], tuple[bytes, str]] = fetcher
        else:
            per_fetch = fetch_budget_s if left is None else max(min(fetch_budget_s, left), 1e-6)
            fetch = partial(default_fetcher, timeout_s=per_fetch)
        data, media_type, failure = _fetch(url, fetch)
        if data is None:
            records.append(
                SourceRecord(
                    url=url,
                    claim=claim,
                    retrieved=False,
                    excerpt=None,
                    sha256=None,
                    content_type=None,
                    retrieved_utc=None,
                    reason=f"unverified_claim: {failure}",
                )
            )
            continue
        digest = sha256_bytes(data)
        earlier = prior.get(url)
        if earlier is not None and earlier.sha256 == digest:
            # Identical bytes were retrieved before: keep that reading and stamp
            # (re-running an unchanged project rewrites the same report).
            records.append(
                SourceRecord(
                    url=url,
                    claim=claim,
                    retrieved=True,
                    excerpt=earlier.excerpt,
                    sha256=digest,
                    content_type=earlier.content_type or media_type,
                    retrieved_utc=earlier.retrieved_utc or now,
                    reason=earlier.reason,
                )
            )
            continue
        excerpt, limitation = _excerpt_of(data, media_type or "")
        records.append(
            SourceRecord(
                url=url,
                claim=claim,
                retrieved=True,
                excerpt=excerpt,
                sha256=digest,
                content_type=media_type or None,
                retrieved_utc=now,
                reason=None if limitation is None else f"excerpt_unavailable: {limitation}",
            )
        )

    retrieved = tuple(record for record in records if record.retrieved)
    if refused_urls and not attempted:
        return finish(
            "unavailable",
            f"no_vendor_source_found: {len(refused_urls)} candidate(s) offered, none on the "
            f"part vendor's sites ({_host_list(allowed_hosts)}); first: {records[0].url}: "
            f"{records[0].reason}",
            records,
        )
    if not retrieved:
        first = records[0]
        return finish(
            "unavailable",
            f"no_source_retrieved: {len(records)} candidate(s) attempted, none could be used "
            f"(first: {first.url}: {first.reason})",
            records,
        )
    return finish(
        "ok",
        f"ok: {len(retrieved)} of {len(records)} candidate source(s) retrieved",
        records,
        _caveats(retrieved),
        _suggested_probes(retrieved),
    )
