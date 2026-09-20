"""API-key author backends: one pasted key, one HTTP call per authoring turn.

This backend is a *proposal source* exactly like Bob Shell: it may write files
inside its sandbox and nothing it reports is evidence — the harness re-runs the
real simulator on whatever the files actually contain. What differs is the
transport: a raw API key from the OS keyring (or ``BOARDMODELER_<NAME>_API_KEY``)
speaks the provider's documented HTTP shape directly, with no login flow and no
local CLI.

Three wires are implemented, each following the vendor's published request shape
(:mod:`boardmodeler.agent_providers` carries the endpoint, the model id and the
documentation URL that were verified — nothing here guesses either):

``openai``
    ``POST <endpoint>/chat/completions`` with ``Authorization: Bearer <key>``.
``anthropic``
    ``POST <endpoint>/messages`` with ``x-api-key`` and ``anthropic-version``.
``google``
    ``POST <endpoint>/models/<model>:generateContent`` with ``x-goog-api-key``.

Reply handling is strict. In the default mode the assistant's text must be exactly
one JSON object whose ``files`` member maps each relative path to that file's
complete text; a markdown fence, a preamble or a partly-formed payload is
``api_reply_unparsed``/``api_reply_invalid`` — no fence stripping, no brace
hunting and no partial writes, because a guessed payload would be an unverified
model. With ``AuthorRequest.expect_text`` the reply text is returned untouched in
``AuthorResult.stdout_tail`` and nothing is written at all. Only
``choices[0].message.content`` (its per-wire equivalent) is ever the reply: a
reasoning field is not an answer. When a reply cannot be parsed, the detail also
names the provider's own stop reason, so an operator can tell a reply that was cut
off at the model's output-token limit from one that was malformed.

This module uses the shared pieces of :mod:`boardmodeler.providers.http_inference`
(``HttpRequest``/``HttpResponse``, ``Transport``, ``urllib_transport``,
``RETRYABLE_STATUSES``, ``ProviderError``) but not its ``chat_completion`` helper:
that helper requires the assistant message to be a JSON object — text mode must
return whatever the model said, prose included — and it returns only the parsed
object, discarding the response envelope whose ``finish_reason``/``stop_reason``
is what separates truncation from a malformed reply. One local request path keeps
both observable.

Every write is validated to stay inside ``AuthorRequest.model_dir`` *as the OS
resolves it*: absolute paths, drive letters, NTFS ``:`` streams, control
characters and any component Windows rewrites — a trailing dot or space, such as
``'.. '``, which ``Path.resolve()`` keeps literal while Win32 canonicalizes it to
the parent — are refused before the containment check, so a path that reaches the
write is one the OS cannot redirect. A write that still cannot be performed is
returned as ``api_write_failed: ...``, never raised. Files are written atomically
through a temporary file plus ``os.replace``. Secrets only ever sit in the auth
header: every detail and ``stdout_tail`` passes through
:func:`boardmodeler.security.credentials.redact`, so a server that echoes the key
back cannot put it into a log, a manifest or a card.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from boardmodeler.agent_providers import AgentProvider, by_id, default_provider, ids
from boardmodeler.authoring.backends import (
    AuthorBackend,
    AuthorRequest,
    AuthorResult,
    BobShellBackend,
    UnavailableBackend,
    stdout_tail,
)
from boardmodeler.config import AppConfig, load_config
from boardmodeler.providers.http_inference import (
    RETRYABLE_STATUSES,
    HttpRequest,
    HttpResponse,
    ProviderError,
    Transport,
    urllib_transport,
)
from boardmodeler.security.credentials import (
    Credential,
    SecretSource,
    credential_key,
    env_var_name,
    get_credential,
    redact,
)

__all__ = [
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "HTTP_WIRES",
    "MAX_OUTPUT_TOKENS",
    "REPLY_FORMAT_INSTRUCTION",
    "ApiKeyBackend",
    "build_api_backend",
    "credential_for",
    "env_sources",
    "parse_files_reply",
]

DEFAULT_TIMEOUT_S = 600.0
"""One authoring turn may take this long, retries included."""

DEFAULT_RETRIES = 2
"""Extra attempts for transport failures and :data:`RETRYABLE_STATUSES`."""

MAX_OUTPUT_TOKENS = 32768
"""Default room for one reply: a ``.lib`` plus its ``.asy`` in one message.

The deliverables are a few thousand tokens of text, and a reasoning-class model
spends several thousand more before it writes any of it — with a smaller budget
such a model returns an empty ``content`` and the turn is lost. ``agent_max_tokens``
in the settings (or ``--max-tokens``) overrides this for a machine or a run whose
provider publishes a different ceiling.
"""

TEMPERATURE = 0.0
"""Deterministic authoring: the same prompt should give the same proposal."""

HTTP_WIRES: tuple[str, ...] = ("openai", "anthropic", "google")
"""Wires this backend speaks; anything else is refused, never coerced."""

ANTHROPIC_VERSION = "2023-06-01"
"""The messages-API version this module implements."""

REPLY_FORMAT_INSTRUCTION = (
    "REPLY FORMAT (required): reply with one JSON object and nothing else — no prose, no "
    "markdown fence, no comments. Its single member is 'files', mapping each path you write "
    "(relative, under the workspace) to that file's complete text, exactly:\n"
    '{"files": {"<relative path>": "<complete file text>"}}'
)
"""Appended to the prompt when the reply must carry files."""

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_MAX_ERROR_BODY = 400
_MAX_BACKOFF_S = 8.0


def _sleep(seconds: float) -> None:
    """Backoff between attempts; a module-level seam so tests never wait."""
    time.sleep(seconds)


def _backoff_s(attempt: int) -> float:
    return min(2.0 ** (attempt - 1), _MAX_BACKOFF_S)


def parse_files_reply(text: str) -> tuple[dict[str, str] | None, str]:
    """``(files, problem)``: the documented reply shape, taken strictly.

    ``files`` is ``None`` with a ``api_reply_unparsed``/``api_reply_invalid``
    problem unless ``text`` is exactly one JSON object with a non-empty ``files``
    map of relative path to file text. A fenced block is *not* unwrapped: guessing
    what the model meant is what produces unverified models.
    """
    stripped = text.strip()
    if not stripped:
        return None, "api_reply_unparsed: the assistant message was empty"
    try:
        document = json.loads(stripped)
    except ValueError as exc:
        return None, f"api_reply_unparsed: the reply is not one JSON object ({exc})"
    if not isinstance(document, dict):
        return None, (
            f"api_reply_invalid: the reply is a {type(document).__name__}, expected a JSON "
            "object with a 'files' map"
        )
    files = document.get("files")
    if not isinstance(files, Mapping) or not files:
        return None, "api_reply_invalid: the reply has no non-empty 'files' map of path to text"
    parsed: dict[str, str] = {}
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            return None, (
                "api_reply_invalid: every 'files' entry must map a path string to text, got "
                f"{type(name).__name__} -> {type(content).__name__}"
            )
        parsed[name] = content
    return parsed, ""


def _numeric_usage(raw: object) -> dict[str, float]:
    """Provider-native numeric usage fields, never converted between units."""
    usage: dict[str, float] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[str(key)] = float(value)
    return usage


def _reply_text(wire: str, payload: Mapping[str, Any]) -> tuple[str, str | None]:
    """``(assistant text, stop reason)`` for one decoded response.

    Only the message's own content is ever the reply — a reasoning/thinking field
    is not an answer. An empty content is a ``ProviderError`` whose message names
    the stop reason when there is one, because "the model spent its whole output
    budget before answering" is a different problem from "the endpoint misbehaved".
    """
    if wire == "openai":
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        stop = first.get("finish_reason") if isinstance(first, Mapping) else None
        reason = stop if isinstance(stop, str) and stop else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                "response_empty", _empty_reply_reason("openai", "the assistant message", reason)
            )
        return content, reason
    if wire == "anthropic":
        blocks = payload.get("content")
        texts = [
            block.get("text")
            for block in blocks or []
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        joined = "\n".join(text for text in texts if text.strip())
        stop = payload.get("stop_reason")
        reason = stop if isinstance(stop, str) and stop else None
        if not joined.strip():
            raise ProviderError(
                "response_empty", _empty_reply_reason("anthropic", "the response", reason)
            )
        return joined, reason
    if wire == "google":
        candidates = payload.get("candidates")
        first = candidates[0] if isinstance(candidates, list) and candidates else None
        content = first.get("content") if isinstance(first, Mapping) else None
        parts = content.get("parts") if isinstance(content, Mapping) else None
        texts = [
            part.get("text")
            for part in parts or []
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        joined = "\n".join(text for text in texts if text.strip())
        stop = first.get("finishReason") if isinstance(first, Mapping) else None
        reason = stop if isinstance(stop, str) and stop else None
        if not joined.strip():
            raise ProviderError(
                "response_empty", _empty_reply_reason("google", "the candidate", reason)
            )
        return joined, reason
    raise ProviderError(
        "wire_unsupported", f"{wire!r} is not a wire this build speaks: {', '.join(HTTP_WIRES)}"
    )


def _empty_reply_reason(wire: str, subject: str, stop: str | None) -> str:
    text = f"{subject} carries no text content to parse"
    note = _limit_note(wire, stop)
    if note:
        return f"{text}; {note}"
    return f"{text} (stop reason {stop!r})" if stop else text


def _limit_note(wire: str, stop: str | None) -> str:
    """The provider's own words for "I stopped at the output-token limit", or ``""``."""
    if wire == "openai" and stop == "length":
        return "the model stopped at its output-token limit (finish_reason='length')"
    if wire == "anthropic" and stop == "max_tokens":
        return "the model stopped at its output-token limit (stop_reason='max_tokens')"
    if wire == "google" and stop == "MAX_TOKENS":
        return "the model stopped at its output-token limit (finishReason='MAX_TOKENS')"
    return ""


def _truncation_note(wire: str, stop: str | None) -> str:
    """A factual note when the provider says it stopped at its output limit."""
    note = _limit_note(wire, stop)
    return f"; {note}, so the reply was cut off rather than malformed" if note else ""


def _excerpt(response: HttpResponse) -> str:
    text = " ".join(response.text().split())
    return text[:_MAX_ERROR_BODY] + ("..." if len(text) > _MAX_ERROR_BODY else "")


def env_sources(provider: AgentProvider) -> tuple[str, ...]:
    """Every environment variable a key for ``provider`` may come from, in order.

    The first entry is the ``BOARDMODELER_<NAME>_API_KEY`` fallback the shared
    credential helper reads; the rest are the vendor's own variables declared by
    the catalog. :func:`credential_for` and the missing-key reason both read this
    one tuple, so a message can never advertise a variable nothing reads.
    """
    return (env_var_name(provider.credential), *provider.env_aliases)


def credential_for(
    provider: AgentProvider, lookup: Callable[[str], Credential] | None = None
) -> Credential:
    """The key for ``provider``: keyring, ``BOARDMODELER_<NAME>_API_KEY``, then aliases.

    ``lookup`` is the repo helper (:func:`boardmodeler.security.credentials.get_credential`
    by default, and the injectable seam tests use); the catalog's own environment
    variables are read here, in order, and the matching variable is named in the
    returned ``detail``. Public because ``doctor`` must report the *same*
    resolution the backend performs — a doctor that contradicts a working build is
    worse than no doctor. Nothing here ever puts a value into a returned text.
    """
    credential = (lookup or get_credential)(provider.credential)
    if credential.value:
        return credential
    for variable in env_sources(provider)[1:]:
        value = os.environ.get(variable)
        if value:
            return Credential(
                name=provider.credential,
                value=value,
                source=SecretSource.ENV,
                detail=f"environment variable {variable}",
            )
    return credential


class ApiKeyBackend:
    """One provider from :data:`boardmodeler.agent_providers.CATALOG`, spoken over HTTP.

    ``provider`` is a catalog entry (never a hand-built shape the build does not
    accept: :meth:`availability` refuses an id outside the catalog). ``model``
    overrides the provider's documented default and ``max_output_tokens`` its
    output budget (:data:`MAX_OUTPUT_TOKENS` when neither the caller nor the
    settings name one). ``timeout_s`` bounds one whole turn, retries included;
    ``retries`` counts extra attempts; ``transport`` is injectable so tests never
    reach the network; and ``credential_lookup`` is the key source (the repo
    keyring/``BOARDMODELER_*_API_KEY`` helpers plus the catalog's aliases by
    default).
    """

    def __init__(
        self,
        provider: AgentProvider,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        transport: Transport | None = None,
        credential_lookup: Callable[[str], Credential] | None = None,
    ) -> None:
        if provider.wire not in HTTP_WIRES:
            raise ValueError(
                f"ApiKeyBackend speaks {', '.join(HTTP_WIRES)}; provider {provider.id!r} uses "
                f"the {provider.wire!r} wire — build it through build_api_backend instead"
            )
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
        if retries < 0:
            raise ValueError(f"retries must be >= 0, got {retries}")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError(f"max_output_tokens must be > 0 or None, got {max_output_tokens}")
        self.provider = provider
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.transport: Transport = transport or urllib_transport
        self.credential_lookup: Callable[[str], Credential] = credential_lookup or get_credential

    # ------------------------------------------------------------- contract

    @property
    def name(self) -> str:
        """The backend name recorded on the card and in ``results.json``."""
        return self.provider.id

    def availability(self) -> tuple[bool, str]:
        """``(usable, reason)``; the reason names every source and never a value."""
        if by_id(self.provider.id) is None:
            accepted = ", ".join(repr(name) for name in ids()) or "none"
            return False, (
                f"api_provider_unavailable: {self.provider.id!r} is not a provider this build "
                f"accepts; use one of {accepted}"
            )
        endpoint = self._endpoint()
        if not endpoint:
            return False, (
                f"api_endpoint_not_configured: provider {self.provider.id!r} ships no endpoint "
                f"in this build; document one from {self.provider.docs}"
            )
        model = self._model()
        if not model:
            return False, (
                f"api_model_not_configured: provider {self.provider.id!r} ships no default "
                f"model; pass --model (documented values: {self.provider.docs})"
            )
        credential = self._credential()
        if credential.value is None:
            return False, self._missing_key_reason()
        return True, (
            f"{self.provider.label} at {endpoint}; model {model}; credential from "
            f"{credential.source.value.lower()}"
            + (f" ({credential.detail})" if credential.detail else "")
        )

    def author(
        self,
        request: AuthorRequest,
        cancel: threading.Event | None = None,
        *,
        timeout_s: float | None = None,
    ) -> AuthorResult:
        """One HTTP authoring turn. A failed or cancelled turn is returned, never raised."""
        if cancel is not None and cancel.is_set():
            return self._failed("cancelled: the API turn was not started, the build was cancelled")
        if timeout_s is not None and float(timeout_s) <= 0:
            return self._failed(f"api_request_failed: timeout_s must be > 0, got {timeout_s!r}")
        usable, reason = self.availability()
        if not usable:
            return self._failed(reason)
        key = self._credential().value or ""
        prompt = request.prompt
        if not request.expect_text:
            prompt = f"{prompt.rstrip()}\n\n{REPLY_FORMAT_INSTRUCTION}"
        url, headers, body = self._shape(prompt, key=key)
        limit = self.timeout_s if timeout_s is None else float(timeout_s)
        try:
            text, usage, stop = self._exchange(
                url=url, headers=headers, body=body, key=key, cancel=cancel, timeout_s=limit
            )
        except ProviderError as exc:
            if exc.code == "cancelled":
                return self._failed("cancelled: the API request was stopped before it completed")
            # A model whose own switch is what let it answer can still spend the whole
            # budget reasoning and return nothing. The entry declares the setting that
            # turns that off, so re-ask once with it rather than losing the turn.
            if exc.code == "response_empty" and self.provider.retry_body:
                retry_url, retry_headers, retry_body = self._shape(
                    prompt, key=key, extra_body=self.provider.retry_body
                )
                try:
                    text, usage, stop = self._exchange(
                        url=retry_url,
                        headers=retry_headers,
                        body=retry_body,
                        key=key,
                        cancel=cancel,
                        timeout_s=limit,
                    )
                except ProviderError as again:
                    return self._failed(
                        redact(
                            f"api_request_failed: {exc.code}: {exc.detail}; the retry with "
                            f"the provider's fallback setting failed: {again.code}: {again.detail}",
                            [key],
                        )
                    )
                except Exception as again:  # a transport that raises is still recorded
                    return self._failed(
                        redact(
                            f"api_request_failed: {exc.code}: {exc.detail}; the retry with "
                            f"the provider's fallback setting failed: {type(again).__name__}: {again}",
                            [key],
                        )
                    )
            else:
                return self._failed(redact(f"api_request_failed: {exc.code}: {exc.detail}", [key]))
        except Exception as exc:  # a transport that raises is still a recorded failure
            return self._failed(redact(f"api_request_failed: {type(exc).__name__}: {exc}", [key]))
        if request.expect_text:
            return AuthorResult(
                ok=True,
                detail=redact(
                    f"api_text: {self.provider.id} replied {len(text)} character(s)", [key]
                ),
                usage=usage,
                stdout_tail=redact(text, [key]),
                session_id=None,
            )
        files, problem = parse_files_reply(text)
        if files is None:
            # One retry inside the same turn. A reply that is not the required JSON object
            # is a transcription slip rather than a verdict, and the parse error is the
            # most useful correction to hand back; the harness still decides everything,
            # and a retry that also fails is reported with both attempts named.
            retry_prompt = (
                f"{prompt}\n\nYour previous reply was rejected: {problem}\n"
                "Reply again with exactly one JSON object and nothing else."
            )
            retry_url, retry_headers, retry_body = self._shape(retry_prompt, key=key)
            try:
                again_text, again_usage, again_stop = self._exchange(
                    url=retry_url,
                    headers=retry_headers,
                    body=retry_body,
                    key=key,
                    cancel=cancel,
                    timeout_s=limit,
                )
            except ProviderError as exc:
                problem = f"{problem}; the retry failed: {exc.code}"
            except Exception as exc:
                problem = f"{problem}; the retry failed: {type(exc).__name__}"
            else:
                retried, retried_problem = parse_files_reply(again_text)
                usage = {
                    name: usage.get(name, 0.0) + again_usage.get(name, 0.0)
                    for name in set(usage) | set(again_usage)
                }
                text, stop = again_text, again_stop
                if retried is not None:
                    files, problem = retried, ""
                else:
                    problem = f"{problem}; the retry was rejected too: {retried_problem}"
            if files is None:
                return AuthorResult(
                    ok=False,
                    detail=redact(problem + _truncation_note(self.provider.wire, stop), [key]),
                    usage=usage,
                    stdout_tail=stdout_tail(text, secrets=[key]),
                    session_id=None,
                )
        targets, problem = self._targets(request.model_dir, files)
        if targets is None:
            return AuthorResult(
                ok=False,
                detail=redact(problem + _truncation_note(self.provider.wire, stop), [key]),
                usage=usage,
                stdout_tail=stdout_tail(text, secrets=[key]),
                session_id=None,
            )
        try:
            for target, content in targets:
                _write_atomically(target, content)
        except (OSError, ValueError) as exc:
            return AuthorResult(
                ok=False,
                detail=redact(
                    f"api_write_failed: {type(exc).__name__}: {exc}; the model files may be "
                    "incomplete — an earlier file of this reply may already be on disk, and "
                    "the harness judges whatever is there",
                    [key],
                ),
                usage=usage,
                stdout_tail=stdout_tail(text, secrets=[key]),
                session_id=None,
            )
        return AuthorResult(
            ok=True,
            detail=redact(
                f"api_files: {self.provider.id} wrote {len(targets)} file(s) under "
                f"{Path(request.model_dir).name}/",
                [key],
            ),
            usage=usage,
            stdout_tail=stdout_tail(text, secrets=[key]),
            session_id=None,
        )

    # ------------------------------------------------------------ internals

    def _credential(self) -> Credential:
        """The key for this provider, from the shared :func:`credential_for` walk."""
        return credential_for(self.provider, self.credential_lookup)

    def _missing_key_reason(self) -> str:
        """Why the key is missing: names every source, never a value."""
        sources = env_sources(self.provider)
        return (
            f"api_key_unavailable: credential {self.provider.credential!r} has no value; store "
            f"it in the OS keyring (service 'boardmodeler', key "
            f"{credential_key(self.provider.credential)!r}) or set {' or '.join(sources)}"
        )

    def _endpoint(self) -> str:
        return str(self.provider.endpoint or "").strip().rstrip("/")

    def _model(self) -> str:
        return str(self.model or self.provider.model or "").strip()

    def _max_tokens(self) -> int:
        return self.max_output_tokens or MAX_OUTPUT_TOKENS

    def _shape(
        self, prompt: str, *, key: str, extra_body: Mapping[str, object] | None = None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """``(url, headers, body)`` for one turn, in the provider's documented shape.

        ``extra_body`` replaces the entry's own switches for this request only; the
        fallback re-ask uses it to send the setting that makes a model answer at all.
        """
        endpoint = self._endpoint()
        model = self._model()
        budget = self._max_tokens()
        wire = self.provider.wire
        if wire == "openai":
            body: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            if self.provider.reasoning:
                body["max_completion_tokens"] = budget
            else:
                body["temperature"] = TEMPERATURE
                body["max_tokens"] = budget
            # The entry's own documented switches last, so a vendor knob can override a
            # default above (DeepSeek's ``thinking`` switch is the one that matters here).
            body.update(dict(self.provider.extra_body if extra_body is None else extra_body))
            return (
                f"{endpoint}/chat/completions",
                {"Authorization": f"Bearer {key}"},
                body,
            )
        if wire == "anthropic":
            return (
                f"{endpoint}/messages",
                {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
                {
                    "model": model,
                    "max_tokens": budget,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        return (
            f"{endpoint}/models/{model}:generateContent",
            {"x-goog-api-key": key},
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": TEMPERATURE,
                    "maxOutputTokens": budget,
                },
            },
        )

    def _exchange(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        key: str,
        cancel: threading.Event | None,
        timeout_s: float,
    ) -> tuple[str, dict[str, float], str | None]:
        """One turn as ``(assistant text, usage, stop reason)``.

        One request path for every wire and both modes: the response envelope has
        to stay visible, because the reply text may be arbitrary (text mode) and
        because a truncated reply is only distinguishable from a malformed one by
        the provider's own stop reason.
        """
        payload = self._post_json(
            url=url, headers=headers, body=body, key=key, cancel=cancel, timeout_s=timeout_s
        )
        usage = (
            payload.get("usageMetadata") if self.provider.wire == "google" else payload.get("usage")
        )
        text, stop = _reply_text(self.provider.wire, payload)
        return text, _numeric_usage(usage), stop

    def _post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        key: str,
        cancel: threading.Event | None,
        timeout_s: float,
    ) -> dict[str, Any]:
        """POST one JSON body with the same retry policy as the shared path.

        ``timeout_s`` bounds the whole turn, retries included: each attempt gets
        what is left of it, and an attempt is refused once the budget is gone.
        """
        deadline = time.monotonic() + timeout_s
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        total_attempts = self.retries + 1
        attempts = 0
        last_detail = "no attempt was made"
        last_status: int | None = None
        while attempts < total_attempts:
            attempts += 1
            if cancel is not None and cancel.is_set():
                raise ProviderError("cancelled", f"cancelled before attempt {attempts}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    "timeout",
                    f"the {timeout_s:g} s budget for this turn was exhausted before attempt "
                    f"{attempts} of {total_attempts}",
                )
            request = HttpRequest(
                method="POST",
                url=url,
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=encoded,
                timeout_s=min(timeout_s, remaining),
            )
            response: HttpResponse | None = None
            try:
                response = self.transport(request)
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {redact(str(exc), [key])}"
            if response is not None:
                if 200 <= response.status < 300:
                    return _decoded(response, secrets=[key])
                last_status = response.status
                last_detail = f"HTTP {response.status}: {redact(_excerpt(response), [key])}"
                if response.status not in RETRYABLE_STATUSES:
                    code = "http_auth_error" if response.status in (401, 403) else "http_error"
                    raise ProviderError(code, last_detail)
            if attempts < total_attempts:
                _sleep(_backoff_s(attempts))
        code = "http_error" if last_status is not None else "transport_error"
        raise ProviderError(code, f"{last_detail} (after {attempts} of {total_attempts} attempts)")

    def _targets(
        self, model_dir: Path, files: Mapping[str, str]
    ) -> tuple[list[tuple[Path, str]] | None, str]:
        """``([(path, text)], problem)`` with every path proven inside ``model_dir``."""
        root = Path(model_dir)
        if not root.name:
            return None, f"api_write_refused: {str(root)!r} is not a model directory"
        targets: list[tuple[Path, str]] = []
        for name, content in files.items():
            target, problem = _relative_target(root, name)
            if target is None:
                return None, f"api_write_refused: {problem}; nothing was written"
            targets.append((target, content))
        return targets, ""

    def _failed(self, detail: str) -> AuthorResult:
        return AuthorResult(ok=False, detail=detail, usage={}, stdout_tail="", session_id=None)


def _decoded(response: HttpResponse, *, secrets: list[str]) -> dict[str, Any]:
    """One JSON-object response body, or a redacted ``ProviderError``."""
    text = response.text()
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ProviderError(
            "response_not_json",
            f"the endpoint returned a body that is not JSON: {redact(text[:200], secrets)}",
        ) from exc
    if not isinstance(document, dict):
        raise ProviderError(
            "response_not_json",
            f"the endpoint returned a {type(document).__name__}, expected a JSON object",
        )
    return document


def _relative_target(model_dir: Path, name: str) -> tuple[Path | None, str]:
    """``(absolute target, problem)`` for one reply path, or ``(None, why)``.

    The prompt names the deliverables as ``model/<SUBCKT>.lib`` relative to the
    workspace, so a leading segment equal to the model directory's own name is
    the same file and is accepted; everything else must be a plain relative path
    that stays inside the directory. Every component is refused before the
    containment check when the OS would resolve it differently from
    ``Path.resolve()``: a trailing dot or space (Win32 strips it, so ``'.. '`` is
    ``..``) and control characters (an embedded NUL cannot be written at all).
    """
    raw = name.replace("\\", "/").strip()
    if not raw:
        return None, "the reply names an empty path"
    if raw.startswith("/") or _DRIVE_LETTER.match(raw):
        return None, f"{name!r} is an absolute path"
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None, f"{name!r} escapes the model directory with '..'"
    if any(any(char < " " for char in part) for part in parts):
        return None, f"{name!r} contains a control character"
    if any(part[-1:] in (".", " ") for part in parts):
        return None, f"{name!r} has a component Windows rewrites (trailing dot or space)"
    if any(":" in part for part in parts):
        return None, f"{name!r} contains ':'"
    if len(parts) > 1 and parts[0] == model_dir.name:
        parts = parts[1:]
    if not parts:
        return None, f"{name!r} names no file"
    root = model_dir.resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        return None, f"{name!r} resolves outside {root}"
    return target, ""


def _write_atomically(target: Path, content: str) -> None:
    """Write ``content`` so a reader sees either the old file or the whole new one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def build_api_backend(
    provider_id: str | None = None,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    team_id: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    config: AppConfig | None = None,
) -> AuthorBackend:
    """The backend the configured provider names, or one that says why not.

    Resolution is explicit and never falls back: an explicit ``provider_id``, else
    ``config.agent_provider``, else :func:`boardmodeler.agent_providers.default_provider`.
    A provider id this build does not accept returns an unavailable backend naming
    the ids it does — never another provider. ``bob`` is the Bob CLI, whose key is
    read by the child process, so it yields a
    :class:`~boardmodeler.authoring.backends.BobShellBackend` of its own (``model``
    and ``max_tokens`` do not apply to it; ``team_id`` is passed to it). ``model``
    follows the same order:
    explicit, ``config.agent_model``, the provider's documented default; so does
    ``max_tokens`` (explicit, ``config.agent_max_tokens``, and
    :data:`MAX_OUTPUT_TOKENS` inside the backend when neither is set).
    """
    if config is None:
        try:
            config = load_config()
        except Exception as exc:  # a malformed config must not stop the caller
            return UnavailableBackend(
                "api",
                f"agent_config_unreadable: {type(exc).__name__}: {exc}; fix or remove the file "
                "and re-run",
            )
    explicit = None if provider_id is None else str(provider_id).strip()
    wanted = explicit or str(config.agent_provider or "").strip() or default_provider().id
    provider = by_id(wanted)
    if provider is None:
        accepted = ", ".join(repr(name) for name in ids()) or "none"
        return UnavailableBackend(
            wanted,
            f"api_provider_unavailable: {wanted!r} is not a provider this build accepts; "
            f"use one of {accepted}",
        )
    if provider.wire not in HTTP_WIRES:
        return BobShellBackend(team_id=team_id, timeout_s=timeout_s)
    resolved_model = model or str(config.agent_model or "").strip() or None
    resolved_tokens = max_tokens if max_tokens is not None else config.agent_max_tokens
    return ApiKeyBackend(
        provider,
        model=resolved_model,
        max_output_tokens=resolved_tokens,
        timeout_s=timeout_s,
    )
