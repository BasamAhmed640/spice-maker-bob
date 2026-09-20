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

from boardmodeler.authoring.probes import PROBES
from boardmodeler.domain.hashing import sha256_bytes

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
        data = json.loads(text)
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


def build_candidate_prompt(part: str, max_sources: int) -> str:
    """The read-only question the agent backend is asked for candidate URLs."""
    shape = (
        '{"sources": [{"url": "https://...", "claim": "one line: what this source '
        'shows about the part"}]}'
    )
    return (
        f"List up to {max_sources} public sources a SPICE model author should read about the "
        f"exact part number {part!r}: errata and silicon advisories, application notes, "
        "thermal and layout guidance, vendor SPICE-model release notes and their known caveats.\n"
        "Answer with one JSON object and nothing else, in exactly this shape:\n"
        f"{shape}\n"
        "Rules: only URLs you are confident exist; never guess or construct a URL from a "
        "pattern; one short line per claim; do not repeat a URL; no prose outside the JSON "
        "object."
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
    cancel: threading.Event | None = None,
) -> ReinforcementReport:
    """Find supporting sources around a model build and record what was retrieved.

    Writes ``<out_dir>/spec/supporting.json`` (indent 2, sorted keys, LF) and
    returns the same report. ``backend`` is the build's own author backend — the
    search then runs on the agent the author loop uses; without it the configured
    provider is built. ``timeout_s`` bounds the whole search (``None``
    leaves it unbounded); when it expires the stage is ``unavailable`` with a
    reason naming the budget and the build continues. ``cancel`` is the build's
    cancellation event, passed to the candidate agent turn. Statuses:

    * ``skipped`` — ``enabled`` is False; zero candidate lookups, zero fetches;
    * ``unavailable`` — enabled, but nothing could be retrieved (no candidates,
      every fetch refused, only unreadable bodies, or the search budget ran out).
      Never a failure: the build continues, and every reason is on its source
      record;
    * ``ok`` — at least one candidate's bytes were retrieved and hashed.

    This function never raises for a retrieval, agent or provider problem; only
    a filesystem error while writing the report can propagate.
    """
    out_root = Path(out_dir)
    deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)

    def budget_left() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def budget_reason() -> str:
        return (
            "search_budget_exceeded: the supporting-material search did not finish within "
            f"{float(timeout_s):g} s"
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

    if candidate_provider is None:
        with tempfile.TemporaryDirectory(prefix="boardmodeler-reinforce-") as scratch:
            reply, note = query_agent_backend(
                build_candidate_prompt(part, max_sources),
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
    for url, claim in candidates:
        left = budget_left()
        if left == 0.0:
            return finish("unavailable", budget_reason())
        if fetcher is not None:
            fetch: Callable[[str], tuple[bytes, str]] = fetcher
        else:
            per_fetch = (
                float(fetch_timeout_s)
                if left is None
                else max(min(float(fetch_timeout_s), left), 1e-6)
            )
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
