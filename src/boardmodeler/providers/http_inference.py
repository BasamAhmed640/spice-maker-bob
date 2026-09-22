"""JSON chat-completion HTTP inference provider (D11, A8).

The adapter is deliberately boring: it POSTs the chat-completions request shape
that :func:`build_chat_body` produces, reads the assistant message, and hands the
parsed JSON object to the caller — which validates it against the D4 schemas
(:mod:`boardmodeler.requirements.extract`). It never guesses an endpoint, never
guesses a model string, and never reads a secret out of configuration text:
``ProviderConfig.endpoint``/``.model`` must have been observed in vendor
documentation (D-005), and the API key comes from this folder's plain local credential file or
``BOARDMODELER_<NAME>_API_KEY`` through :func:`boardmodeler.security.credentials.get_credential`.

Transport, TLS, and secrets:

* Requests go through an injectable :data:`Transport`. Production uses
  :func:`urllib_transport`, which honours the default trust store,
  ``REQUESTS_CA_BUNDLE``/``SSL_CERT_FILE``, and the usual proxy environment
  variables (``HTTPS_PROXY``/``HTTP_PROXY``/``NO_PROXY``). Tests inject a
  transport or point the adapter at a local server — never at a live endpoint.
* A credential value is only ever placed in the auth header; every error message
  is passed through :func:`boardmodeler.security.credentials.redact`, so a
  server echo of the key cannot leak into a log or a manifest.

Failure behaviour: retries are bounded by ``ProviderConfig.retries`` and only
apply to transport errors and the retryable status set (429/5xx); 401/403 fail
immediately as ``http_auth_error`` and any other status as ``http_error``. A
response that is not a JSON object, or whose assistant message is not a JSON
object, fails as ``response_not_json`` rather than being coerced. Egress is
catalog-bound: :func:`require_vendor_endpoint` refuses a destination outside the
selected entry's own documented host before any header, credential lookup or
socket, so no retry or redirect can reach it.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from boardmodeler.agent_providers import endpoint_is_vendor
from boardmodeler.config import ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.domain.records import ProviderIdentity
from boardmodeler.providers.base import (
    MAX_SNIPPET_CHARS,
    DocSnippet,
    ExtractionRequest,
    ExtractionResponse,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    request_hash,
)
from boardmodeler.security.credentials import env_var_name, get_credential, redact

__all__ = [
    "SYSTEM_PROMPT",
    "ChatResult",
    "HttpInferenceProvider",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "build_chat_body",
    "chat_completion",
    "extract_json_object",
    "probe_endpoint",
    "render_user_content",
    "require_vendor_endpoint",
    "urllib_transport",
]

SYSTEM_PROMPT = (
    "You extract structured engineering data. Reply with a single JSON object that matches the "
    "requested schema and nothing else: no prose, no markdown fence, no commentary."
)

RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
"""Statuses worth a second attempt; everything else fails immediately."""

_MAX_ERROR_BODY = 400
_MAX_BACKOFF_S = 8.0


@dataclass(frozen=True)
class HttpRequest:
    """One HTTP exchange, as the transport sees it (no secret is stripped here)."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    timeout_s: float
    progress: Callable[[str], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class HttpResponse:
    """The transport's answer; a non-2xx status is data, not an exception."""

    status: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Transport = Callable[[HttpRequest], HttpResponse]


@dataclass(frozen=True)
class ChatResult:
    """One successfully parsed chat-completion call."""

    payload: dict[str, Any]
    usage: dict[str, float]
    text: str
    attempts: int


# --------------------------------------------------------------------------- #
# transport


def require_vendor_endpoint(provider: str | None, url: str) -> None:
    """Refuse a URL that is not the selected provider's documented vendor host.

    The check is the catalog's own :func:`boardmodeler.agent_providers.endpoint_is_vendor`
    and it runs before any header, credential lookup or socket: a destination outside
    the vendor cannot be reached by a retry, a redirect or a fallback. A loopback
    destination for a provider this build has no catalog entry for stays allowed —
    that is a local fixture, not internet egress. This edition's one entry is the Bob
    CLI, which declares no HTTP endpoint at all, so naming it refuses every URL: there
    is no wire here to carry an inference request to Bob.
    """
    allowed, reason = endpoint_is_vendor(provider, url)
    if not allowed:
        raise ProviderError("endpoint_not_vendor", reason)


def _ssl_context() -> ssl.SSLContext:
    """Default-verified TLS context, honouring an explicit CA bundle."""
    cafile = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or None
    return ssl.create_default_context(cafile=cafile)


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward provider credentials through redirects.
        return None


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Production transport: stdlib ``urllib`` with system TLS and proxy settings.

    Connection failures raise (so the retry loop can see them); HTTP error
    responses are returned as :class:`HttpResponse` with their status intact.
    """
    http_request = urllib.request.Request(
        request.url,
        data=request.body or None,
        headers=dict(request.headers),
        method=request.method,
    )
    opener = urllib.request.build_opener(
        _NoCredentialRedirect(),
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.ProxyHandler(),
    )
    deadline = time.monotonic() + request.timeout_s
    try:
        with opener.open(http_request, timeout=request.timeout_s) as response:
            return HttpResponse(
                status=int(response.status),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=_read_response_body(response, deadline, progress=request.progress),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = _read_response_body(exc, deadline)
        except Exception:  # the error body is best effort, never the reason we fail
            body = b""
        finally:
            exc.close()
        return HttpResponse(
            status=int(exc.code),
            headers={str(key): str(value) for key, value in (exc.headers or {}).items()},
            body=body or b"",
        )


def _read_response_body(response, deadline, *, progress=None):
    """Enforce a whole-response deadline even while SSE keeps the socket alive."""
    chunks, size = [], 0
    headers = getattr(response, "headers", {})
    streaming = "text/event-stream" in str(headers.get("Content-Type", "")).lower()
    line_buffer = b""
    reported = 0.0
    if progress:
        progress("API connected; waiting for response data")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the API response exceeded its total time budget")
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read1(65536)
        if time.monotonic() >= deadline:
            raise TimeoutError("the API response exceeded its total time budget")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > 128 * 1024 * 1024:
            raise ValueError("API response exceeds the 128 MiB transport bound")
        chunks.append(chunk)
        if progress and time.monotonic() - reported >= 1:
            progress(
                f"API receiving {'stream' if streaming else 'response'}: {size / 1024:.1f} KiB"
            )
            reported = time.monotonic()
        if streaming:
            line_buffer += chunk
            while b"\n" in line_buffer:
                line, line_buffer = line_buffer.split(b"\n", 1)
                if line.rstrip(b"\r").strip() == b"data: [DONE]":
                    if progress:
                        progress("API stream complete; decoding response")
                    # The decoder still requires finish_reason and validates every event.
                    # A server may keep the connection open after completing this stream.
                    return b"".join(chunks)


def probe_endpoint(
    transport: Transport,
    *,
    url: str,
    headers: Mapping[str, str],
    timeout_s: float,
    secrets: Sequence[str] = (),
) -> ProviderHealth:
    """Reachability probe: any answer below HTTP 500 means the endpoint is usable.

    A 401/403/405 is a reachable endpoint that refused this particular request —
    the probe reports that as healthy, because selection must not claim more than
    it measured, and the first real call reports the authorization outcome.
    """
    request = HttpRequest(
        method="GET", url=url, headers=dict(headers), body=b"", timeout_s=timeout_s
    )
    started = time.monotonic()
    try:
        response = transport(request)
    except Exception as exc:  # transport-specific exception types are environment detail
        return ProviderHealth(
            ok=False,
            code="endpoint_unreachable",
            detail=f"GET {url} failed: {type(exc).__name__}: {redact(str(exc), secrets)}",
        )
    latency_ms = (time.monotonic() - started) * 1000.0
    if response.status >= 500:
        return ProviderHealth(
            ok=False,
            code="endpoint_error",
            detail=f"GET {url} returned HTTP {response.status}",
            latency_ms=latency_ms,
        )
    return ProviderHealth(
        ok=True,
        code="ok",
        detail=(
            f"GET {url} answered HTTP {response.status} "
            "(reachability only; authorization is proven by the first call)"
        ),
        latency_ms=latency_ms,
    )


# --------------------------------------------------------------------------- #
# request / response shape


def render_user_content(prompt: str, snippets: Sequence[DocSnippet]) -> str:
    """Deterministic prompt text: the instruction, then each page with its label.

    Snippets are labelled with their document id and 0-based page so the model
    can cite a page, and so a missing page is visible in the request rather than
    being quietly absent. Shared by the HTTP/Bob adapters and Bob Shell.
    """
    parts: list[str] = [prompt.rstrip(), ""]
    for snippet in snippets:
        label = f"page {snippet.pdf_page}"
        if snippet.printed_label:
            label += f" (printed {snippet.printed_label})"
        parts.append(f"--- {snippet.doc_id} | {label} ---")
        parts.append(snippet.text)
        parts.append("")
    return "\n".join(parts)


def build_chat_body(
    *,
    model: str,
    prompt: str,
    snippets: Sequence[DocSnippet],
    max_output_tokens: int,
    temperature: float = 0.0,
    json_mode: bool = True,
    system: str | None = None,
) -> dict[str, Any]:
    """Deterministic chat-completions body for one extraction request."""
    if not model:
        raise ValueError("model must be a non-empty string")
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": render_user_content(prompt, snippets)},
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def chat_completion(
    body: Mapping[str, Any],
    *,
    transport: Transport,
    url: str,
    headers: Mapping[str, str],
    timeout_s: float,
    retries: int,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    secrets: Sequence[str] = (),
    provider: str | None = None,
) -> ChatResult:
    """POST ``body`` to ``url`` and return the parsed assistant payload.

    Retries transport failures and :data:`RETRYABLE_STATUSES` up to ``retries``
    extra attempts, waiting between attempts via ``sleep`` (injectable so tests
    do not spend wall-clock time). Cancellation is checked before every attempt.

    ``provider`` names the catalog entry the caller selected; when it is given,
    the destination is checked against that vendor's documented host *before*
    the first attempt, so a misdirected endpoint raises
    ``endpoint_not_vendor`` instead of being sent and retried.
    """
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")
    if not url:
        raise ValueError("url must be a non-empty string")
    if provider is not None:
        require_vendor_endpoint(provider, url)
    encoded = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
    request = HttpRequest(
        method="POST",
        url=url,
        headers={
            **dict(headers),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body=encoded,
        timeout_s=timeout_s,
    )
    total_attempts = retries + 1
    attempts = 0
    last_detail = "no attempt was made"
    last_status: int | None = None
    while attempts < total_attempts:
        attempts += 1
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled", f"cancelled before attempt {attempts}")
        response: HttpResponse | None = None
        try:
            response = transport(request)
        except Exception as exc:
            last_detail = f"{type(exc).__name__}: {redact(str(exc), secrets)}"
        if response is not None:
            if 200 <= response.status < 300:
                return _decode(response, secrets=secrets, attempts=attempts)
            last_status = response.status
            last_detail = f"HTTP {response.status}: {redact(_body_excerpt(response), secrets)}"
            if response.status not in RETRYABLE_STATUSES:
                code = "http_auth_error" if response.status in (401, 403) else "http_error"
                raise ProviderError(code, last_detail)
        if attempts < total_attempts:
            sleep(_backoff_s(attempts))
    code = "http_error" if last_status is not None else "transport_error"
    raise ProviderError(code, f"{last_detail} (after {attempts} of {total_attempts} attempts)")


def _backoff_s(attempt: int) -> float:
    return min(2.0 ** (attempt - 1), _MAX_BACKOFF_S)


def _body_excerpt(response: HttpResponse) -> str:
    text = " ".join(response.text().split())
    return text[:_MAX_ERROR_BODY] + ("..." if len(text) > _MAX_ERROR_BODY else "")


def _decode(response: HttpResponse, *, secrets: Sequence[str], attempts: int) -> ChatResult:
    text = response.text()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "response_not_json",
            f"the endpoint returned a body that is not JSON: {redact(text[:200], secrets)}",
        ) from exc
    if not isinstance(document, dict):
        raise ProviderError(
            "response_not_json",
            f"the endpoint returned a {type(document).__name__}, expected a JSON object",
        )
    usage = _usage(document.get("usage"))
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(
            "response_no_choices",
            f"the response carries no choices: {redact(text[:200], secrets)}",
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(
            "response_empty", "the assistant message carries no text content to parse"
        )
    return ChatResult(
        payload=extract_json_object(content, secrets=secrets),
        usage=usage,
        text=content,
        attempts=attempts,
    )


def _usage(raw: object) -> dict[str, float]:
    usage: dict[str, float] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[str(key)] = float(value)
    return usage


def extract_json_object(text: str, *, secrets: Sequence[str] = ()) -> dict[str, Any]:
    """Parse the JSON object out of an assistant message.

    A markdown fence is stripped (models add one even when told not to); anything
    else that is not exactly one JSON object is a ``response_not_json`` error —
    no brace hunting, because a guessed payload would be an unverified answer.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Redact the whole reply before taking an excerpt: slicing a secret first
        # leaves a fragment that exact-match redaction cannot recognize.
        safe_text = redact(stripped, secrets)
        safe_pos = len(redact(stripped[: exc.pos], secrets))
        raise ProviderError(
            "response_not_json",
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}; "
            f"near {safe_text[max(0, safe_pos - 80) : safe_pos + 120]!r}",
        ) from exc
    if not isinstance(document, dict):
        raise ProviderError(
            "response_not_json",
            f"the assistant message is a {type(document).__name__}, expected a JSON object",
        )
    return document


# --------------------------------------------------------------------------- #
# provider


class HttpInferenceProvider:
    """A configured JSON chat-completion chat-completions endpoint."""

    def __init__(
        self,
        *,
        provider_config: ProviderConfig,
        name: str,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("provider name must be non-empty")
        self.provider_config = provider_config
        self.name = name
        self.transport: Transport = transport or urllib_transport
        self.sleep: Callable[[float], None] = sleep or time.sleep
        self.system_prompt = system_prompt or SYSTEM_PROMPT

    # ------------------------------------------------------------- contract

    def identity(self, *, usage: Mapping[str, float] | None = None) -> ProviderIdentity:
        """Who ran. Usage is reported in provider-native units (tokens)."""
        return ProviderIdentity(
            provider=self.name,
            kind=ProviderKind.HTTP_INFERENCE,
            model=self.provider_config.model,
            endpoint=self.provider_config.endpoint,
            usage_units="tokens",
            usage=dict(usage or {}),
            detail={
                "auth_header": self.provider_config.auth_header,
                "json_mode": "response_format=json_object",
            },
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="tokens",
            notes=(
                "JSON chat-completion chat completions with response_format=json_object; "
                "the payload is validated against the D4 schemas by the caller"
            ),
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        """Static configuration plus a reachability probe (no inference call)."""
        problem = self._configuration_problem()
        if problem is not None:
            return problem
        try:
            require_vendor_endpoint(self.name, str(self.provider_config.endpoint))
        except ProviderError as exc:
            return ProviderHealth(ok=False, code=exc.code, detail=exc.detail)
        try:
            headers, secrets = self._auth_headers()
        except ProviderError as exc:
            return ProviderHealth(ok=False, code=exc.code, detail=exc.detail)
        return probe_endpoint(
            self.transport,
            url=str(self.provider_config.endpoint),
            headers=headers,
            timeout_s=timeout_s,
            secrets=secrets,
        )

    def extract(
        self, request: ExtractionRequest, cancel: threading.Event | None = None
    ) -> ExtractionResponse:
        """One chat-completions call; the caller validates the payload."""
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled", f"cancelled before {request.task.value} extraction")
        endpoint = self._require_endpoint()
        model = self._require_model()
        require_vendor_endpoint(self.name, endpoint)
        headers, secrets = self._auth_headers()
        body = build_chat_body(
            model=model,
            prompt=request.prompt,
            snippets=request.snippets,
            max_output_tokens=self.provider_config.max_output_tokens,
            temperature=self.provider_config.temperature,
            system=self.system_prompt,
        )
        result = chat_completion(
            body,
            transport=self.transport,
            url=endpoint,
            headers=headers,
            timeout_s=self.provider_config.timeout_s,
            retries=self.provider_config.retries,
            cancel=cancel,
            sleep=self.sleep,
            secrets=secrets,
            provider=self.name,
        )
        return ExtractionResponse(
            payload=result.payload,
            identity=self.identity(usage=result.usage),
            raw_text=result.text,
            from_cache=False,
            request_hash=request_hash(request, provider=self.name, model=model),
            detail=f"HTTP chat completion in {result.attempts} attempt(s)",
        )

    # ------------------------------------------------------------ internals

    def _configuration_problem(self) -> ProviderHealth | None:
        if not self.provider_config.endpoint:
            return ProviderHealth(
                ok=False,
                code="endpoint_not_configured",
                detail=(
                    f"provider {self.name!r} has no endpoint; set ProviderConfig.endpoint from "
                    "vendor documentation (never guess it)"
                ),
            )
        if not self.provider_config.model:
            return ProviderHealth(
                ok=False,
                code="model_not_configured",
                detail=(
                    f"provider {self.name!r} has no model; set ProviderConfig.model from vendor "
                    "documentation (never guess it)"
                ),
            )
        return None

    def _require_endpoint(self) -> str:
        endpoint = self.provider_config.endpoint
        if not endpoint:
            raise ProviderError(
                "endpoint_not_configured",
                f"provider {self.name!r} has no endpoint; set ProviderConfig.endpoint from "
                "vendor documentation (never guess it)",
            )
        return endpoint

    def _require_model(self) -> str:
        model = self.provider_config.model
        if not model:
            raise ProviderError(
                "model_not_configured",
                f"provider {self.name!r} has no model; set ProviderConfig.model from vendor "
                "documentation (never guess it)",
            )
        return model

    def _auth_headers(self) -> tuple[dict[str, str], list[str]]:
        """The auth header plus the secrets to redact (never logged anywhere)."""
        credential = get_credential(self.name)
        if credential.value is None:
            raise ProviderError(
                "credential_missing",
                f"provider {self.name!r} has no credential: {credential.detail}. "
                "Store it in the plain local credential file in this folder "
                "(boardmodeler / provider:<name>:api_key) or set "
                f"{env_var_name(self.name)}.",
            )
        header = self.provider_config.auth_header or "Authorization"
        scheme = self.provider_config.auth_scheme.strip()
        value = f"{scheme} {credential.value}".strip() if scheme else credential.value
        return {header: value}, [credential.value]
