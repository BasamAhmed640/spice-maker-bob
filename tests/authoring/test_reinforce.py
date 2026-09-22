"""Internet reinforcement: retrieved bytes are the only fact the stage records.

Every test here is offline. Candidate discovery goes through an injected
provider or a monkeypatched agent seam, and retrieval through an injected
fetcher or a fake urllib opener. What is being defended is the honesty
contract: a source that was not fetched stays an ``unverified_claim``, a
malformed agent reply yields no candidates, a non-text content type is refused
with a reason, and no retrieval path may raise out of :func:`reinforce`.
"""

from __future__ import annotations

import email.message
import json
import threading
import urllib.error
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from boardmodeler.authoring import reinforce as reinforce_module
from boardmodeler.authoring.reinforce import (
    FetchRefused,
    ReinforcementReport,
    build_candidate_prompt,
    default_fetcher,
    parse_agent_reply,
    reinforce,
    vendor_hosts,
)
from boardmodeler.domain.hashing import sha256_bytes
from boardmodeler.domain.records import DocumentRecord

PART = "TPS54320"
DIGEST = "d" * 64
#: The datasheet's own provenance: the part vendor's site, and the only kind of
#: host the stage may fetch from once a document records it.
VENDOR_SOURCE_URL = "https://www.ti.com/lit/ds/symlink/tps54320.pdf"
#: A candidate source on the vendor's own site.
URL = "https://www.ti.com/lit/an/tps54320-errata"
REPORT_NAME = Path("spec") / "supporting.json"
FROZEN_UTC = "2026-09-18T12:00:00Z"


def _write_provenance(out_dir: Path) -> None:
    """Record the datasheet the project was analysed from, which names the vendor."""
    docs = Path(out_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    record = DocumentRecord(
        doc_id="DOC_TPS54320",
        title="TPS54320 datasheet",
        manufacturer="Texas Instruments",
        doc_type="datasheet",
        file_hash="0" * 64,
        source_url=VENDOR_SOURCE_URL,
        provenance="user_supplied",
        classification="public",
    )
    (docs / "DOC_TPS54320.json").write_text(record.model_dump_json(), encoding="utf-8")


@pytest.fixture(autouse=True)
def vendor_provenance(tmp_path: Path) -> None:
    """Give every build the datasheet provenance the stage derives its allowlist from."""
    _write_provenance(tmp_path)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frozen retrieval timestamp: byte-stable reports are the point of it."""
    frozen = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(reinforce_module, "_utc_now", lambda: frozen.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _fetcher(body: bytes, content_type: str, calls: list[str] | None = None):
    def fetch(url: str) -> tuple[bytes, str]:
        if calls is not None:
            calls.append(url)
        return body, content_type

    return fetch


def _provider(*pairs: tuple[str, str]):
    def provide(part: str) -> list[tuple[str, str]]:
        assert part == PART
        return list(pairs)

    return provide


def _report_text(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / REPORT_NAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# (a) a retrieved source is verbatim text with its own hash and url


def test_retrieved_source_carries_verbatim_excerpt_hash_and_url(tmp_path: Path) -> None:
    body = (
        b"<html>\n<body>\nErrata: the TPS54320 must not exceed 17 V on VIN.\n"
        b"See the workaround in section 4.</body>\n</html>"
    )
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "errata and workaround for TPS54320")),
        fetcher=_fetcher(body, "text/html; charset=utf-8"),
    )

    assert report.status == "ok"
    assert report.enabled is True
    assert report.spec_digest == DIGEST
    assert report.detail == "ok: 1 of 1 candidate source(s) retrieved"
    record = report.sources[0]
    assert record.url == URL
    assert record.claim == "errata and workaround for TPS54320"
    assert record.retrieved is True
    assert record.reason is None
    assert record.content_type == "text/html"
    assert record.retrieved_utc == FROZEN_UTC
    assert record.sha256 == sha256_bytes(body)
    assert record.excerpt == " ".join(body.decode().split())
    assert report.caveats == (
        f"{URL}: <html> <body> Errata: the TPS54320 must not exceed 17 V on VIN.",
        f"{URL}: See the workaround in section 4.</body> </html>",
    )


def test_report_file_is_written_and_round_trips(tmp_path: Path) -> None:
    body = b"UVLO threshold is 4.3 V; soft start is 2 ms."
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "startup behaviour")),
        fetcher=_fetcher(body, "text/plain"),
    )

    text = (tmp_path / REPORT_NAME).read_text(encoding="utf-8")
    assert text == report.to_json()
    assert ReinforcementReport.from_json(text) == report
    payload = json.loads(text)
    assert sorted(payload) == [
        "caveats",
        "detail",
        "enabled",
        "part",
        "sources",
        "spec_digest",
        "status",
        "suggested_probes",
    ]
    assert sorted(payload["sources"][0]) == [
        "claim",
        "content_type",
        "excerpt",
        "reason",
        "retrieved",
        "retrieved_utc",
        "sha256",
        "url",
    ]
    assert payload["sources"][0]["sha256"] == sha256_bytes(body)
    assert payload["sources"][0]["excerpt"] == body.decode()
    # A deterministic report is written with LF on every platform.
    assert b"\r\n" not in (tmp_path / REPORT_NAME).read_bytes()


def test_excerpt_is_capped_at_4000_characters(tmp_path: Path) -> None:
    body = ("word " * 2000).encode()  # 10 000 characters before collapsing
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "long note")),
        fetcher=_fetcher(body, "text/plain"),
    )
    record = report.sources[0]
    assert record.retrieved is True
    assert record.excerpt is not None
    assert len(record.excerpt) == 4000
    assert record.excerpt.startswith("word word word")


# --------------------------------------------------------------------------- #
# (b) an unreachable source is recorded, never raised


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("the read operation timed out"), "unverified_claim: timeout:"),
        (
            urllib.error.HTTPError(URL, 404, "Not Found", None, None),
            "unverified_claim: http_error: HTTP 404 Not Found",
        ),
        (urllib.error.URLError("name or service not known"), "unverified_claim: url_error:"),
        (
            FetchRefused("content_type_refused: 'text/csv'"),
            "unverified_claim: content_type_refused: 'text/csv'",
        ),
    ],
)
def test_unreachable_source_is_recorded_with_its_reason(
    tmp_path: Path, error: BaseException, expected: str
) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise error

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "errata")),
        fetcher=fetch,
    )

    assert report.status == "unavailable"
    assert report.detail.startswith("no_source_retrieved: 1 candidate(s) attempted")
    record = report.sources[0]
    assert record.url == URL
    assert record.retrieved is False
    assert record.excerpt is None
    assert record.sha256 is None
    assert record.content_type is None
    assert record.retrieved_utc is None
    assert record.reason is not None and record.reason.startswith(expected)
    assert report.caveats == ()
    assert report.suggested_probes == ()
    assert _report_text(tmp_path)["status"] == "unavailable"


def test_redirect_exhaustion_is_reported_as_a_redirect_limit(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise urllib.error.HTTPError(url, 302, "Found", None, None)

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "redirects")),
        fetcher=fetch,
    )
    assert report.sources[0].reason == (
        "unverified_claim: redirect_limit: stopped after 3 redirects (HTTP 302)"
    )


def test_claim_is_preserved_but_labelled_unverified(tmp_path: Path) -> None:
    claim = "errata: this part is not recommended for new designs"

    def fetch(url: str) -> tuple[bytes, str]:
        raise TimeoutError("timed out")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, claim)),
        fetcher=fetch,
    )
    assert report.sources[0].claim == claim
    assert report.sources[0].reason.startswith("unverified_claim:")


# --------------------------------------------------------------------------- #
# (c) disabled: a skipped report and zero fetches


def test_disabled_writes_skipped_report_and_makes_zero_fetches(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("the fetcher must not run when the stage is disabled")

    def provider(part: str) -> list[tuple[str, str]]:
        raise AssertionError("candidate discovery must not run when the stage is disabled")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=provider,
        fetcher=fetch,
        enabled=False,
    )
    assert report.status == "skipped"
    assert report.enabled is False
    assert report.sources == ()
    assert report.caveats == ()
    assert report.detail.startswith("disabled:")
    assert _report_text(tmp_path)["status"] == "skipped"


def test_max_sources_zero_lists_nothing_and_fetches_nothing(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("nothing was requested, so nothing may be fetched")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "errata")),
        fetcher=fetch,
        max_sources=0,
    )
    assert report.status == "unavailable"
    assert report.sources == ()
    assert report.detail.startswith("max_sources_invalid:")


# --------------------------------------------------------------------------- #
# (d) the agent path: strict replies, malformed input is no candidates


@pytest.mark.parametrize(
    "reply",
    [
        "I could not find anything useful for this part.",
        "",
        '{"sources": "none"}',
        '{"sources": [{"url": "https://example.invalid/a"}]}',
        '{"sources": [{"url": "ftp://example.invalid/a", "claim": "c"}]}',
        '{"sources": [{"url": "not a url", "claim": "c"}]}',
        (
            '{"sources": [{"url": "https://example.invalid/a", "claim": "ok"}, '
            '{"url": "broken", "claim": "c"}]}'
        ),
        "{}",
    ],
)
def test_malformed_agent_reply_yields_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    monkeypatch.setattr(
        reinforce_module, "query_agent_backend", lambda prompt, workdir, **kwargs: (reply, "")
    )

    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("a malformed reply must not lead to a fetch")

    report = reinforce(
        part=PART, spec_digest=DIGEST, out_dir=tmp_path, fetcher=fetch, max_sources=6
    )
    assert report.status == "unavailable"
    assert report.sources == ()
    assert report.detail.startswith("candidate_reply")


def test_agent_reply_candidates_are_fetched_and_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = json.dumps({"sources": [{"url": URL, "claim": "UVLO and soft start notes"}]})
    monkeypatch.setattr(
        reinforce_module, "query_agent_backend", lambda prompt, workdir, **kwargs: (reply, "")
    )
    body = b"UVLO threshold is 4.3 V typical."

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        fetcher=_fetcher(body, "text/plain"),
    )
    assert report.status == "ok"
    assert report.sources[0].sha256 == sha256_bytes(body)
    assert report.suggested_probes == ("uvlo_rise",)


def test_parse_agent_reply_accepts_a_nested_json_answer() -> None:
    inner = json.dumps({"sources": [{"url": URL, "claim": "a note"}]})
    outer = json.dumps({"type": "result", "result": inner})
    assert parse_agent_reply(outer, 6) == ((URL, "a note"),)


def test_parse_agent_reply_drops_nothing_when_the_reply_is_valid() -> None:
    text = json.dumps(
        {
            "sources": [
                {"url": "https://example.invalid/b", "claim": "second"},
                {"url": "https://example.invalid/a", "claim": "first"},
                {"url": "https://example.invalid/a", "claim": "duplicate"},
            ]
        }
    )
    assert parse_agent_reply(text, 6) == (
        ("https://example.invalid/b", "second"),
        ("https://example.invalid/a", "first"),
    )


def test_agent_backend_note_becomes_the_unavailable_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = "agent_backend_unavailable: bob_shell_not_installed: install bob"
    monkeypatch.setattr(
        reinforce_module, "query_agent_backend", lambda prompt, workdir, **kwargs: ("", note)
    )
    report = reinforce(part=PART, spec_digest=DIGEST, out_dir=tmp_path)
    assert report.status == "unavailable"
    assert report.detail == note
    assert report.sources == ()


def test_query_agent_backend_uses_the_injected_backend_for_a_text_turn(
    tmp_path: Path,
) -> None:
    from boardmodeler.authoring.backends import AuthorRequest, AuthorResult

    seen: dict[str, object] = {}

    class _FakeBackend:
        name = "fake"

        def availability(self) -> tuple[bool, str]:
            return True, "ok"

        def author(
            self,
            request: AuthorRequest,
            cancel: threading.Event | None = None,
            *,
            timeout_s: float | None = None,
        ) -> AuthorResult:
            seen["cancel"] = cancel
            seen["prompt"] = request.prompt
            seen["expect_text"] = request.expect_text
            seen["model_dir"] = request.model_dir
            seen["author_timeout_s"] = timeout_s
            return AuthorResult(ok=True, detail="ok", usage={}, stdout_tail="{}", session_id=None)

    event = threading.Event()

    reply, note = reinforce_module.query_agent_backend(
        "find sources", tmp_path, backend=_FakeBackend(), cancel=event, timeout_s=12.5
    )

    assert note == "" and reply == "{}"
    assert seen["cancel"] is event, "the build's cancel event must reach the backend"
    assert seen["expect_text"] is True, "a candidate turn must not write files"
    assert seen["model_dir"] == tmp_path
    assert seen["author_timeout_s"] == 12.5, "an injected backend still gets the budget"


def test_query_agent_backend_defaults_to_the_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an injected backend the search runs on the configured provider."""
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.authoring.backends import AuthorRequest, AuthorResult

    seen: dict[str, object] = {}

    class _FakeBackend:
        name = "fake"

        def availability(self) -> tuple[bool, str]:
            return True, "ok"

        def author(
            self,
            request: AuthorRequest,
            cancel: threading.Event | None = None,
            *,
            timeout_s: float | None = None,
        ) -> AuthorResult:
            seen["author_timeout_s"] = timeout_s
            seen["expect_text"] = request.expect_text
            return AuthorResult(ok=True, detail="ok", usage={}, stdout_tail="{}", session_id=None)

    def factory(*, timeout_s: float = 0.0, **kwargs: object) -> _FakeBackend:
        seen["timeout_s"] = timeout_s
        return _FakeBackend()

    monkeypatch.setattr(api_module, "build_api_backend", factory)

    reply, note = reinforce_module.query_agent_backend("find sources", tmp_path, timeout_s=12.5)

    assert note == "" and reply == "{}"
    assert seen["timeout_s"] == 12.5
    assert seen["expect_text"] is True


def test_query_agent_backend_reports_an_unavailable_backend(
    tmp_path: Path,
) -> None:
    from boardmodeler.authoring.backends import UnavailableBackend

    reply, note = reinforce_module.query_agent_backend(
        "find sources", tmp_path, backend=UnavailableBackend("api", "api_key_unavailable: none")
    )

    assert reply == ""
    assert note == "agent_backend_unavailable: api_key_unavailable: none"


def test_reinforce_threads_the_cancel_event_to_the_candidate_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def query(prompt: str, workdir: Path, **kwargs: object) -> tuple[str, str]:
        seen.append(kwargs.get("cancel"))
        return "", "agent_backend_unavailable: none"

    monkeypatch.setattr(reinforce_module, "query_agent_backend", query)
    event = threading.Event()

    report = reinforce(part=PART, spec_digest=DIGEST, out_dir=tmp_path, cancel=event)

    assert seen == [event]
    assert report.status == "unavailable"


def test_an_expired_search_budget_is_recorded_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([1000.0, 2000.0])
    monkeypatch.setattr(reinforce_module.time, "monotonic", lambda: next(ticks, 2000.0))

    def provider(part: str) -> list[tuple[str, str]]:
        raise AssertionError("an expired budget must not query candidates")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=provider,
        timeout_s=5.0,
    )

    assert report.status == "unavailable"
    assert report.detail.startswith("search_budget_exceeded:")
    assert "5 s" in report.detail
    assert report.sources == ()


class _CountsTurns:
    """A backend that records every turn, so a test can prove none was taken."""

    def __init__(self) -> None:
        self.turns = 0

    def author(self, request, cancel=None, *, timeout_s=None):
        self.turns += 1
        raise AssertionError(
            "the search was given a budget too small for a turn, so it must not start one"
        )


def test_a_budget_too_small_for_one_turn_gives_up_without_spending_it(
    tmp_path: Path,
) -> None:
    """The recorded 1.4.0 build paid its whole allowance and got nothing back.

    The search's first step is one agent turn, and a reasoning-class model needs
    minutes for it, while ``reinforce_timeout_s`` defaulted to 45 s. The budget was
    therefore arithmetically unspendable: every build spent 45 s and received
    ``search_budget_exceeded``. Declining before the spend is the fix — the reason
    has to distinguish "too small to try" from "tried and ran out", because only the
    first is something the user can act on.
    """
    backend = _CountsTurns()

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        backend=backend,
        timeout_s=reinforce_module.MIN_AGENT_TURN_S - 1.0,
    )

    assert report.status == "unavailable"
    assert report.detail.startswith("search_budget_too_small:"), report.detail
    assert "reinforce_timeout_s" in report.detail, "the reason must name the setting"
    assert backend.turns == 0, "a budget too small to try must not spend anything"
    assert report.sources == ()


def test_candidate_prompt_states_the_part_limit_and_the_json_shape() -> None:
    prompt = build_candidate_prompt(PART, 4)
    assert PART in prompt
    assert "up to 4 public sources" in prompt
    assert '"sources"' in prompt
    assert "never guess or construct a URL" in prompt


# --------------------------------------------------------------------------- #
# (e) byte-stable, round-trippable JSON


def test_same_inputs_produce_byte_identical_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"Quiescent current is 1.2 mA typical."
    provider = _provider((URL, "quiescent current"))
    fetcher = _fetcher(body, "text/plain")
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_provenance(first)
    _write_provenance(second)

    report_a = reinforce(
        part=PART, spec_digest=DIGEST, out_dir=first, candidate_provider=provider, fetcher=fetcher
    )
    report_b = reinforce(
        part=PART, spec_digest=DIGEST, out_dir=second, candidate_provider=provider, fetcher=fetcher
    )
    assert report_a == report_b
    assert (first / REPORT_NAME).read_bytes() == (second / REPORT_NAME).read_bytes()
    assert ReinforcementReport.from_json(report_a.to_json()) == report_a
    assert report_a.suggested_probes == ("quiescent_current",)

    # A later re-run of the same bytes rewrites the same report: the earlier
    # retrieval stamp is reused, so only a *changed* upstream moves the file.
    before = (first / REPORT_NAME).read_bytes()
    monkeypatch.setattr(reinforce_module, "_utc_now", lambda: "2030-01-01T00:00:00Z")
    rerun = reinforce(
        part=PART, spec_digest=DIGEST, out_dir=first, candidate_provider=provider, fetcher=fetcher
    )
    assert (first / REPORT_NAME).read_bytes() == before
    assert rerun.sources[0].retrieved_utc == FROZEN_UTC

    # ... and changed bytes move it, with the new stamp.
    changed = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=first,
        candidate_provider=provider,
        fetcher=_fetcher(b"Quiescent current is 1.5 mA typical.", "text/plain"),
    )
    assert changed.sources[0].retrieved_utc == "2030-01-01T00:00:00Z"
    assert (first / REPORT_NAME).read_bytes() != before


# --------------------------------------------------------------------------- #
# (f) non-text content types are refused with a reason


def test_injected_fetcher_content_type_is_refused_with_a_reason(tmp_path: Path) -> None:
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "binary note")),
        fetcher=_fetcher(b"\x00\x01\x02", "application/octet-stream"),
    )
    record = report.sources[0]
    assert record.retrieved is False
    assert record.sha256 is None
    assert record.reason == (
        "unverified_claim: content_type_refused: 'application/octet-stream' "
        "(accepted: text/html, text/plain, application/pdf)"
    )
    assert report.status == "unavailable"


def test_default_fetcher_refuses_non_http_schemes_and_bad_timeouts() -> None:
    with pytest.raises(FetchRefused) as scheme:
        default_fetcher("file:///C:/secret.txt")
    assert "scheme_refused" in str(scheme.value)
    with pytest.raises(FetchRefused) as credentials:
        default_fetcher("https://user:secret@example.invalid/a")
    assert "credentials_in_url" in str(credentials.value)
    with pytest.raises(FetchRefused) as timeout:
        default_fetcher("https://example.invalid/a", timeout_s=0)
    assert "invalid_timeout" in str(timeout.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://0.0.0.0/",
    ],
)
def test_default_fetcher_refuses_non_public_destinations(url: str) -> None:
    with pytest.raises(FetchRefused) as refusal:
        default_fetcher(url)
    assert "host_refused" in str(refusal.value)


def test_default_fetcher_refuses_a_hostname_that_resolves_privately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reinforce_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(FetchRefused) as refusal:
        default_fetcher("https://intranet.example/a")
    assert "host_refused" in str(refusal.value)


def test_default_fetcher_refuses_a_hostname_that_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(*args: object, **kwargs: object) -> list[object]:
        raise OSError("name resolution failed")

    monkeypatch.setattr(reinforce_module.socket, "getaddrinfo", unreachable)
    with pytest.raises(FetchRefused) as refusal:
        default_fetcher("https://nowhere.example/a")
    assert "host_refused" in str(refusal.value)


def test_a_redirect_to_a_non_public_host_is_refused() -> None:
    handler = reinforce_module._RedirectCap()
    request = Request("https://93.184.216.34/a")
    with pytest.raises(FetchRefused) as refusal:
        handler.redirect_request(
            request, None, 302, "Found", email.message.Message(), "http://169.254.169.254/"
        )
    assert "host_refused" in str(refusal.value)


def test_candidate_carrying_credentials_is_never_recorded(tmp_path: Path) -> None:
    # A credentialed URL would be persisted into the report, so it is refused
    # before any fetch: the secret never reaches supporting.json.
    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("a credentialed URL must not be fetched")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider(("https://user:secret@example.invalid/a", "private note")),
        fetcher=fetch,
    )
    assert report.status == "unavailable"
    assert report.sources == ()
    assert report.detail.startswith("candidate_provider_malformed:")
    assert "secret" not in (tmp_path / REPORT_NAME).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the default fetcher, exercised through a fake opener (still no network)


class _FakeResponse:
    def __init__(
        self, body: bytes, content_type: str | None, content_length: str | None = None
    ) -> None:
        headers = email.message.Message()
        if content_type is not None:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = content_length
        self.headers = headers
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.request: Any = None
        self.timeout: float | None = None

    def open(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.request = request
        self.timeout = timeout
        return self.response


def _install_fake_opener(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> _FakeOpener:
    opener = _FakeOpener(response)
    monkeypatch.setattr(reinforce_module, "build_opener", lambda handler: opener)
    return opener


def test_default_fetcher_sends_a_user_agent_and_honours_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _install_fake_opener(monkeypatch, _FakeResponse(b"hello", "text/plain; charset=utf-8"))
    body, content_type = default_fetcher("https://93.184.216.34/a", timeout_s=7.5)
    assert body == b"hello"
    assert content_type == "text/plain"
    assert opener.timeout == 7.5
    headers = {key.lower(): value for key, value in opener.request.header_items()}
    assert headers["user-agent"].startswith("BoardModeler/")


def test_default_fetcher_refuses_a_non_text_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_opener(monkeypatch, _FakeResponse(b"x", "application/octet-stream"))
    with pytest.raises(FetchRefused) as refusal:
        default_fetcher("https://93.184.216.34/a")
    assert "content_type_refused: 'application/octet-stream'" in str(refusal.value)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_FakeResponse(b"x", "text/plain", content_length=str(2 << 20)), "too_large"),
        (_FakeResponse(b"x" * ((1 << 20) + 1), "text/plain"), "too_large"),
    ],
)
def test_default_fetcher_refuses_oversize_bodies(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse, expected: str
) -> None:
    _install_fake_opener(monkeypatch, response)
    with pytest.raises(FetchRefused) as refusal:
        default_fetcher("https://93.184.216.34/a")
    assert expected in str(refusal.value)


# --------------------------------------------------------------------------- #
# what retrieved text may support


def _pdf_bytes(*lines: str) -> bytes:
    """A synthetic PDF with a real text layer; made-up text, not vendor data."""
    buffer = BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=letter)
    for index, line in enumerate(lines):
        sheet.drawString(72, 700 - 20 * index, line)
    sheet.save()
    return buffer.getvalue()


def test_retrieved_pdf_text_layer_is_quoted_from_the_retrieved_bytes(tmp_path: Path) -> None:
    body = _pdf_bytes(
        "Errata: the TPS54320 must not exceed 17 V on VIN.",
        "See section 4 for the workaround.",
    )
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "vendor errata")),
        fetcher=_fetcher(body, "application/pdf"),
    )
    record = report.sources[0]
    assert record.retrieved is True
    assert record.sha256 == sha256_bytes(body)
    assert record.excerpt is not None
    assert "Errata: the TPS54320 must not exceed 17 V on VIN." in record.excerpt
    assert report.caveats == (
        f"{URL}: Errata: the TPS54320 must not exceed 17 V on VIN.",
        f"{URL}: See section 4 for the workaround.",
    )


def test_pdf_without_extractable_text_is_retrieved_but_never_quoted(tmp_path: Path) -> None:
    body = b"%PDF-1.4\nthis is not really a pdf\n%%EOF"
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "vendor model release notes")),
        fetcher=_fetcher(body, "application/pdf"),
    )
    record = report.sources[0]
    assert record.retrieved is True
    assert record.sha256 == sha256_bytes(body)
    assert record.content_type == "application/pdf"
    assert record.excerpt is None
    assert record.reason is not None and record.reason.startswith("excerpt_unavailable:")
    assert report.caveats == ()


def test_empty_body_is_retrieved_but_never_quoted(tmp_path: Path) -> None:
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "empty note")),
        fetcher=_fetcher(b"  \n\t ", "text/plain"),
    )
    record = report.sources[0]
    assert record.retrieved is True
    assert record.excerpt is None
    assert record.reason == (
        "excerpt_unavailable: empty_text: the retrieved body carries no readable text"
    )
    assert report.caveats == ()


def test_suggested_probes_follow_registry_order_not_mention_order(tmp_path: Path) -> None:
    body = b"Power good delay, soft start ramp and the UVLO threshold are all documented here."
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "startup notes")),
        fetcher=_fetcher(body, "text/plain"),
    )
    assert report.suggested_probes == ("uvlo_rise", "pg_threshold", "soft_start")


def test_unretrieved_claim_never_becomes_a_caveat_or_a_probe(tmp_path: Path) -> None:
    claim = "errata: UVLO is not supported below 4 V"

    def fetch(url: str) -> tuple[bytes, str]:
        raise urllib.error.URLError("offline")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, claim)),
        fetcher=fetch,
    )
    assert report.sources[0].claim == claim
    assert report.caveats == ()
    assert report.suggested_probes == ()


def test_caveats_come_only_from_retrieved_sources(tmp_path: Path) -> None:
    reachable = "https://www.ti.com/notes"
    body = b"The output is not supported below 2.5 V."

    def fetch(url: str) -> tuple[bytes, str]:
        if url == reachable:
            return body, "text/plain"
        raise TimeoutError("timed out")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((URL, "not supported below 2.5 V"), (reachable, "note")),
        fetcher=fetch,
    )
    assert report.status == "ok"
    assert report.caveats == (f"{reachable}: The output is not supported below 2.5 V.",)


# --------------------------------------------------------------------------- #
# a broken provider is a reason, never an exception


def test_raising_candidate_provider_is_recorded(tmp_path: Path) -> None:
    def provider(part: str) -> list[tuple[str, str]]:
        raise RuntimeError("author backend exploded")

    report = reinforce(part=PART, spec_digest=DIGEST, out_dir=tmp_path, candidate_provider=provider)
    assert report.status == "unavailable"
    assert report.detail == "candidate_provider_error: RuntimeError: author backend exploded"
    assert report.sources == ()


@pytest.mark.parametrize(
    ("provider", "prefix"),
    [
        (lambda part: [], "candidate_provider_empty:"),
        (lambda part: "not a sequence", "candidate_provider_malformed:"),
        (lambda part: [("https://example.invalid/a",)], "candidate_provider_malformed:"),
    ],
)
def test_unusable_candidate_provider_is_recorded(
    tmp_path: Path, provider: Any, prefix: str
) -> None:
    report = reinforce(part=PART, spec_digest=DIGEST, out_dir=tmp_path, candidate_provider=provider)
    assert report.status == "unavailable"
    assert report.sources == ()
    assert report.detail.startswith(prefix)


def test_max_sources_caps_fetches_and_keeps_candidate_order(tmp_path: Path) -> None:
    pairs = [(f"https://www.ti.com/notes/{index}", f"claim {index}") for index in range(10)]
    calls: list[str] = []
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=lambda part: pairs,
        fetcher=_fetcher(b"a plain note", "text/plain", calls),
        max_sources=3,
    )
    assert report.status == "ok"
    assert calls == [pair[0] for pair in pairs[:3]]
    assert [record.url for record in report.sources] == [pair[0] for pair in pairs[:3]]
    assert [record.claim for record in report.sources] == [pair[1] for pair in pairs[:3]]


# --------------------------------------------------------------------------- #
# egress: the search never leaves the part vendor, and gives up when it would


def test_vendor_hosts_derives_the_allowlist_from_provenance_and_catalog(tmp_path: Path) -> None:
    hosts = vendor_hosts(tmp_path)

    # The datasheet's own source_url names the vendor; the ``www.`` label is noise.
    assert "ti.com" in hosts
    assert "www.ti.com" not in hosts
    # This build's catalog documentation hosts are always part of the allowlist.
    assert "api-docs.deepseek.com" in hosts
    assert "example.invalid" not in hosts
    assert len(hosts) == len(set(hosts)), "hosts stay deduplicated"


def test_a_caller_supplied_vendor_url_widens_the_allowlist(tmp_path: Path) -> None:
    hosts = vendor_hosts(
        tmp_path / "no-provenance-here", extra=("https://www.analog.com/ds/ad8232.pdf",)
    )
    assert "analog.com" in hosts


def test_catalog_documentation_hosts_are_always_allowed(tmp_path: Path) -> None:
    plain = tmp_path / "no-provenance"
    plain.mkdir()
    calls: list[str] = []
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=plain,
        candidate_provider=_provider(("https://api-docs.deepseek.com/notes", "a documented note")),
        fetcher=_fetcher(b"UVLO threshold is 4.3 V typical.", "text/plain", calls),
    )

    assert report.status == "ok"
    assert calls == ["https://api-docs.deepseek.com/notes"]


def test_a_non_vendor_public_host_is_refused_and_reported(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("a non-vendor destination must not be fetched")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider(("https://example.invalid/tps54320-errata", "errata")),
        fetcher=fetch,
    )

    assert report.status == "unavailable"
    assert report.detail.startswith("no_vendor_source_found:")
    assert "example.invalid" in report.detail
    assert "ti.com" in report.detail
    record = report.sources[0]
    assert record.retrieved is False
    assert record.sha256 is None
    assert record.reason is not None
    assert record.reason.startswith("unverified_claim: vendor_refused:")
    # The refusal is reported, not silent: the reason is in the written report.
    written = _report_text(tmp_path)
    assert written["status"] == "unavailable"
    assert written["detail"].startswith("no_vendor_source_found:")
    assert written["sources"][0]["reason"].startswith("unverified_claim: vendor_refused:")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ti.com.evil.test/tps54320",
        "https://evil-ti.com/tps54320",
        "https://notti.com/tps54320",
        "https://ti.com.evil.test/tps54320",
    ],
)
def test_a_lookalike_vendor_host_is_refused(tmp_path: Path, url: str) -> None:
    def fetch(candidate: str) -> tuple[bytes, str]:
        raise AssertionError(f"a lookalike host must not be fetched: {candidate}")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((url, "lookalike")),
        fetcher=fetch,
    )

    assert report.status == "unavailable"
    assert report.sources[0].reason is not None
    assert report.sources[0].reason.startswith("unverified_claim: vendor_refused:")


def test_a_vendor_subdomain_is_fetched(tmp_path: Path) -> None:
    candidate = "https://e2e.ti.com/support/tps54320"
    calls: list[str] = []
    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider((candidate, "vendor forum note")),
        fetcher=_fetcher(b"UVLO threshold is 4.3 V typical.", "text/plain", calls),
    )

    assert report.status == "ok"
    assert calls == [candidate]
    assert report.sources[0].retrieved is True


def test_nothing_vendor_owned_gives_up_without_spending_the_budget(tmp_path: Path) -> None:
    def fetch(url: str) -> tuple[bytes, str]:
        raise AssertionError("no candidate is on the vendor's site, so nothing may be fetched")

    report = reinforce(
        part=PART,
        spec_digest=DIGEST,
        out_dir=tmp_path,
        candidate_provider=_provider(
            ("https://example.invalid/a", "one"), ("https://example.invalid/b", "two")
        ),
        fetcher=fetch,
        timeout_s=45.0,
    )

    assert report.status == "unavailable"
    assert report.detail.startswith("no_vendor_source_found:")
    assert "2 candidate(s) offered" in report.detail
    assert "search_budget_exceeded" not in report.detail
    for record in report.sources:
        assert record.reason is not None
        assert record.reason.startswith(
            "unverified_claim: vendor_refused: host 'example.invalid' is outside the part "
            "vendor's sites"
        )
        assert "ti.com" in record.reason
        assert "api-docs.deepseek.com" in record.reason


def test_the_candidate_prompt_names_the_vendor_hosts() -> None:
    prompt = build_candidate_prompt(PART, 4, ("ti.com", "api-docs.deepseek.com"))

    assert "ti.com" in prompt
    assert "may not leave the vendor" in prompt
