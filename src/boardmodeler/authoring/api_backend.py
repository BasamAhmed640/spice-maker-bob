"""Construct the IBM Bob author backend from its API-key configuration.

The historical module name preserves callers; this edition ships only Bob Shell.
Model selection and reasoning are controlled by Bob. No unsupported reasoning flag
is invented by this adapter.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from boardmodeler.agent_providers import AgentProvider, by_id, default_provider
from boardmodeler.authoring.backends import AuthorBackend, BobShellBackend, UnavailableBackend
from boardmodeler.config import AppConfig, load_config
from boardmodeler.security.credentials import Credential, SecretSource, env_var_name, get_credential

DEFAULT_TIMEOUT_S = 600.0
MAX_OUTPUT_TOKENS = 32768


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
    """The key for ``provider``: encrypted file, ``BOARDMODELER_<NAME>_API_KEY``, then aliases.

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


def build_api_backend(
    provider_id: str | None = None,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    team_id: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    config: AppConfig | None = None,
) -> AuthorBackend:
    """Use Bob or refuse an incompatible setting, without substitution."""
    if config is None:
        try:
            config = load_config()
        except Exception:
            return UnavailableBackend(
                "bob", "agent_config_unreadable: repair the Bob settings file"
            )
    wanted = str(provider_id or config.agent_provider or default_provider().id).strip()
    if by_id(wanted) is None:
        return UnavailableBackend(
            "bob",
            "api_provider_unavailable: this edition accepts IBM Bob only; select USE IBM BOB in SETUP and SAVE",
        )
    return BobShellBackend(team_id=team_id, timeout_s=timeout_s)
