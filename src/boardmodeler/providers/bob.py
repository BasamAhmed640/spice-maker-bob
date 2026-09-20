"""Bob providers (D11, A8).

Two adapters, both explicit and both disabled until the user asks for them:

``BobDirectProvider``
    The Bob HTTP API. It needs a credential (``BOB_API_KEY`` or the encrypted local entry
    ``boardmodeler`` / ``provider:<name>:api_key``) **and** an endpoint/model pair
    taken from official documentation — neither is guessed, and with either
    missing it reports ``bob_credentials_unavailable`` /
    ``bob_endpoint_not_configured`` instead of falling back to another provider
    (AGENTS rule 5). On this machine no ``BOB_*`` credential exists, so the
    provider is expected to stay in its BLOCKED state (D-005).

``BobShellProvider``
    Bob Shell runs *tools* non-interactively, so it is off unless both
    ``DataPolicy.allow_bob_shell`` and an explicit ``allow_bob_shell=True`` are
    present (A8), it refuses documents classified internal/confidential/unknown,
    it runs in an explicitly supplied working directory with an explicitly
    enumerated tool set, and it never guesses Bob Shell's command line — the
    argv template is supplied by the caller (``{prompt}``, ``{tools}``,
    ``{model}``, ``{workdir}`` placeholders). Usage is reported in **turns**, never
    converted to tokens.

Neither adapter invents a request shape: Bob Direct reuses the JSON chat-completion
chat-completions shape (:func:`boardmodeler.providers.http_inference.chat_completion`,
documented in ``docs/DECISIONS.md`` D-005 as requiring verification against Bob's
official documentation before the endpoint is used), and Bob Shell's command line
is data supplied by the operator.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from boardmodeler.config import ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.domain.records import ProviderIdentity
from boardmodeler.providers.base import (
    MAX_SNIPPET_CHARS,
    ExtractionRequest,
    ExtractionResponse,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    request_hash,
)
from boardmodeler.providers.http_inference import (
    Transport,
    build_chat_body,
    chat_completion,
    extract_json_object,
    probe_endpoint,
    render_user_content,
    urllib_transport,
)
from boardmodeler.security.credentials import env_var_name, get_credential, redact
from boardmodeler.security.subprocess_guard import GuardedProcess, run_guarded

__all__ = ["BOB_API_KEY_ENV", "BobDirectProvider", "BobShellProvider"]

BOB_API_KEY_ENV = "BOB_API_KEY"
"""Environment variable the registry also accepts as a Bob credential."""

_REFUSED_CLASSIFICATIONS = frozenset({"internal", "confidential", "unknown"})
_SHELL_TOOLS_PLACEHOLDER = "{tools}"
_SHELL_PROMPT_PLACEHOLDER = "{prompt}"


class _BobCredentials:
    """Where a Bob credential came from, without ever holding it in a message."""

    __slots__ = ("detail", "value")

    def __init__(self, value: str | None, detail: str) -> None:
        self.value = value
        self.detail = detail


def _bob_credential(name: str) -> _BobCredentials:
    credential = get_credential(name)
    if credential.value is not None:
        return _BobCredentials(credential.value, credential.detail)
    environment = os.environ.get(BOB_API_KEY_ENV)
    if environment:
        return _BobCredentials(environment, f"{BOB_API_KEY_ENV} environment variable is set")
    return _BobCredentials(
        None,
        f"{credential.detail}; neither {env_var_name(name)} nor {BOB_API_KEY_ENV} is set",
    )


class BobDirectProvider:
    """The Bob HTTP API, used only with credentials and an approved endpoint."""

    def __init__(
        self,
        *,
        provider_config: ProviderConfig,
        name: str = "bob_direct",
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not name:
            raise ValueError("provider name must be non-empty")
        self.provider_config = provider_config
        self.name = name
        self.transport: Transport = transport or urllib_transport
        self.sleep: Callable[[float], None] = sleep or time.sleep

    # ------------------------------------------------------------- contract

    def identity(self, *, usage: Mapping[str, float] | None = None) -> ProviderIdentity:
        """Who ran. Bob Direct reports usage in tokens, like any chat completion."""
        return ProviderIdentity(
            provider=self.name,
            kind=ProviderKind.BOB_DIRECT,
            model=self.provider_config.model,
            endpoint=self.provider_config.endpoint,
            usage_units="tokens",
            usage=dict(usage or {}),
            detail={"endpoint_source": "ProviderConfig (from official documentation)"},
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="tokens",
            notes="Bob direct; request shape is the JSON chat-completion chat completion (D-005)",
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        """Credentials and configuration first, then a reachability probe."""
        credentials = _bob_credential(self.name)
        if credentials.value is None:
            return ProviderHealth(
                ok=False,
                code="bob_credentials_unavailable",
                detail=f"nothing was sent: {credentials.detail}",
            )
        if not self.provider_config.endpoint or not self.provider_config.model:
            return ProviderHealth(
                ok=False,
                code="bob_endpoint_not_configured",
                detail=(
                    "ProviderConfig.endpoint and .model must both be set from official Bob "
                    "documentation before Bob Direct can be used; nothing was sent"
                ),
            )
        return probe_endpoint(
            self.transport,
            url=str(self.provider_config.endpoint),
            headers=self._auth_headers(credentials.value),
            timeout_s=timeout_s,
            secrets=[credentials.value],
        )

    def extract(
        self, request: ExtractionRequest, cancel: threading.Event | None = None
    ) -> ExtractionResponse:
        """One Bob call; the caller validates the payload against the D4 schemas."""
        credentials = _bob_credential(self.name)
        if credentials.value is None:
            raise ProviderError(
                "bob_credentials_unavailable", f"nothing was sent: {credentials.detail}"
            )
        endpoint = self.provider_config.endpoint
        model = self.provider_config.model
        if not endpoint or not model:
            raise ProviderError(
                "bob_endpoint_not_configured",
                "ProviderConfig.endpoint and .model must both be set from official Bob "
                "documentation; nothing was sent",
            )
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled", f"cancelled before {request.task.value} extraction")
        body = build_chat_body(
            model=model,
            prompt=request.prompt,
            snippets=request.snippets,
            max_output_tokens=self.provider_config.max_output_tokens,
            temperature=self.provider_config.temperature,
        )
        result = chat_completion(
            body,
            transport=self.transport,
            url=endpoint,
            headers=self._auth_headers(credentials.value),
            timeout_s=self.provider_config.timeout_s,
            retries=self.provider_config.retries,
            cancel=cancel,
            sleep=self.sleep,
            secrets=[credentials.value],
        )
        return ExtractionResponse(
            payload=result.payload,
            identity=self.identity(usage=result.usage),
            raw_text=result.text,
            from_cache=False,
            request_hash=request_hash(request, provider=self.name, model=model),
            detail=f"Bob direct call in {result.attempts} attempt(s)",
        )

    def _auth_headers(self, secret: str) -> dict[str, str]:
        header = self.provider_config.auth_header or "Authorization"
        scheme = self.provider_config.auth_scheme.strip()
        return {header: f"{scheme} {secret}".strip() if scheme else secret}


class BobShellProvider:
    """Bob Shell: a local tool-running process, off unless explicitly enabled."""

    def __init__(
        self,
        *,
        provider_config: ProviderConfig,
        name: str = "bob_shell",
        allow_bob_shell: bool = False,
        argv_template: Sequence[str] | None = None,
        tools: Sequence[str] = (),
        workdir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        runner: Callable[..., GuardedProcess] | None = None,
    ) -> None:
        if not name:
            raise ValueError("provider name must be non-empty")
        self.provider_config = provider_config
        self.name = name
        self.allow_bob_shell = bool(allow_bob_shell)
        self.argv_template = list(argv_template or ())
        self.tools = list(tools)
        self.workdir = Path(workdir) if workdir is not None else None
        self.env = dict(env) if env is not None else None
        self.runner: Callable[..., GuardedProcess] = runner or run_guarded

    # ------------------------------------------------------------- contract

    def identity(self, *, turns: float = 0.0, exit_code: str | None = None) -> ProviderIdentity:
        """Who ran. Bob Shell usage is reported in turns and never converted."""
        detail = {
            "tools": ",".join(self.tools),
            "workdir": str(self.workdir) if self.workdir else "",
        }
        if exit_code is not None:
            detail["exit_code"] = exit_code
        return ProviderIdentity(
            provider=self.name,
            kind=ProviderKind.BOB_SHELL,
            model=self.provider_config.model,
            endpoint=None,
            usage_units="turns",
            usage={"turns": float(turns)},
            detail=detail,
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="turns",
            notes=(
                "Bob Shell runs an enumerated tool set non-interactively in one explicit "
                "working directory; usage is counted in turns"
            ),
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        """Static eligibility only: no process is started by a health probe."""
        problem = self._eligibility_problem()
        if problem is not None:
            return ProviderHealth(ok=False, code=problem[0], detail=problem[1])
        credentials = _bob_credential(self.name)
        if credentials.value is None:
            return ProviderHealth(
                ok=False,
                code="bob_credentials_unavailable",
                detail=f"nothing was run: {credentials.detail}",
            )
        return ProviderHealth(
            ok=True,
            code="ok",
            detail=(
                f"enabled with {len(self.tools)} tool(s) in {self.workdir}; "
                f"credential source: {credentials.detail}"
            ),
        )

    def extract(
        self, request: ExtractionRequest, cancel: threading.Event | None = None
    ) -> ExtractionResponse:
        """Run Bob Shell once and parse its stdout as the extraction payload."""
        problem = self._eligibility_problem()
        if problem is not None:
            raise ProviderError(problem[0], problem[1])
        credentials = _bob_credential(self.name)
        if credentials.value is None:
            raise ProviderError(
                "bob_credentials_unavailable", f"nothing was run: {credentials.detail}"
            )

        refused = sorted(
            {
                record.classification
                for record in request.documents
                if record.classification in _REFUSED_CLASSIFICATIONS
            }
        )
        if refused:
            doc_ids = sorted(
                record.doc_id
                for record in request.documents
                if record.classification in _REFUSED_CLASSIFICATIONS
            )
            raise ProviderError(
                "bob_shell_classification_refused",
                f"refusing to start: document(s) {doc_ids} are classified {refused}; Bob Shell is "
                "only used with public or synthetic fixtures",
            )
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled", f"cancelled before {request.task.value} extraction")

        prompt = render_user_content(request.prompt, request.snippets)
        argv = self._argv(prompt)
        uses_stdin = _SHELL_PROMPT_PLACEHOLDER not in " ".join(self.argv_template)
        assert self.workdir is not None
        process = self.runner(
            argv,
            cwd=self.workdir,
            timeout_s=self.provider_config.timeout_s,
            env=self.env,
            input_text=prompt if uses_stdin else None,
        )
        if process.timed_out:
            raise ProviderError(
                "bob_shell_timeout",
                f"Bob Shell did not finish within {self.provider_config.timeout_s:g} s",
            )
        if process.returncode != 0:
            raise ProviderError(
                "bob_shell_failed",
                f"Bob Shell exited {process.returncode}: "
                f"{redact(' '.join(process.stderr.split())[:300], [credentials.value])}",
            )
        payload = extract_json_object(process.stdout, secrets=[credentials.value])
        return ExtractionResponse(
            payload=payload,
            identity=self.identity(turns=1.0, exit_code=str(process.returncode)),
            raw_text=process.stdout,
            from_cache=False,
            request_hash=request_hash(
                request, provider=self.name, model=self.provider_config.model
            ),
            detail=f"Bob Shell turn in {process.wall_s:.2f} s",
        )

    # ------------------------------------------------------------ internals

    def _eligibility_problem(self) -> tuple[str, str] | None:
        if not self.allow_bob_shell:
            return (
                "bob_shell_not_allowed",
                "Bob Shell is disabled: construct it with allow_bob_shell=True after "
                "data_policy.allow_bob_shell and --allow-bob-shell are both set",
            )
        if not self.argv_template:
            return (
                "bob_shell_argv_not_configured",
                "Bob Shell's command line is not configured; supply argv_template with "
                "{prompt}/{tools} placeholders (BoardModeler does not guess it)",
            )
        if not self.tools:
            return (
                "bob_shell_tools_not_enumerated",
                "no tools were enumerated; Bob Shell pre-approves tools, so the set must be "
                "explicit",
            )
        if self.workdir is None or not self.workdir.is_dir():
            return (
                "bob_shell_workdir_missing",
                f"working directory {self.workdir} does not exist; Bob Shell runs in one "
                "explicit working directory",
            )
        if (
            shutil.which(self.argv_template[0]) is None
            and not Path(self.argv_template[0]).is_file()
        ):
            return (
                "bob_shell_not_found",
                f"executable {self.argv_template[0]!r} was not found on PATH",
            )
        return None

    def _argv(self, prompt: str) -> list[str]:
        """Substitute the known placeholders only (a literal ``{`` is left alone)."""
        replacements = {
            _SHELL_PROMPT_PLACEHOLDER: prompt,
            _SHELL_TOOLS_PLACEHOLDER: ",".join(self.tools),
            "{model}": self.provider_config.model or "",
            "{workdir}": str(self.workdir),
        }
        argv: list[str] = []
        for part in self.argv_template:
            for placeholder, value in replacements.items():
                part = part.replace(placeholder, value)
            argv.append(part)
        return argv
