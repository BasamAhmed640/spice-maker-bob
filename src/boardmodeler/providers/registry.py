"""Provider selection (D11, AGENTS rule 5).

Selection is explicit and never falls back. A requested provider is built or
:class:`~boardmodeler.providers.base.ProviderError` is raised with the observed
reason; the automatic walk over ``AppConfig.provider_order`` takes the first
entry whose static requirements hold and whose health probe passes, listing
every rejection when none does.

The fixture provider is always selectable — it needs no credentials and no
network — so an offline machine has a working default; whether fixtures exist
for a particular request is answered per extraction with ``fixture_missing``,
never by silently substituting echoed text. HTTP and Bob modules are imported
lazily and must expose ``HttpInferenceProvider`` / ``BobDirectProvider`` /
``BobShellProvider`` constructed as ``Class(provider_config=<ProviderConfig>,
name=<str>)``.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardmodeler.config import AppConfig, ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.providers.base import Provider, ProviderError, ProviderHealth
from boardmodeler.providers.fixture import FixtureProvider
from boardmodeler.security.credentials import env_var_name, get_credential

__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "ProviderSelection",
    "build_provider",
    "select_provider",
]

DEFAULT_FIXTURE_DIR = Path("fixtures") / "providers"
"""Fixture location relative to the working directory; the pipeline sets this explicitly."""

_BOB_ENV_VAR = "BOB_API_KEY"
_BOB_CREDENTIAL_NAMES: dict[ProviderKind, str] = {
    ProviderKind.BOB_DIRECT: "bob_direct",
    ProviderKind.BOB_SHELL: "bob_shell",
}
_LAZY_PROVIDERS: dict[ProviderKind, tuple[str, str]] = {
    ProviderKind.HTTP_INFERENCE: ("boardmodeler.providers.http_inference", "HttpInferenceProvider"),
    ProviderKind.BOB_DIRECT: ("boardmodeler.providers.bob", "BobDirectProvider"),
    ProviderKind.BOB_SHELL: ("boardmodeler.providers.bob", "BobShellProvider"),
}


@dataclass(frozen=True)
class ProviderSelection:
    """A chosen provider plus why it was chosen (recorded in the manifest/card)."""

    provider: Provider
    kind: ProviderKind
    detail: str


def _entry_for_kind(config: AppConfig, kind: ProviderKind) -> tuple[str, ProviderConfig]:
    for name, provider_config in config.providers.items():
        if provider_config.kind is kind:
            return name, provider_config
    raise ProviderError(
        "provider_not_configured",
        f"no entry in config.providers has kind {kind.value}; configured: {sorted(config.providers)}",
    )


def _import_optional(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ProviderError(
            "provider_not_implemented",
            f"{module_name} is not available in this build: {type(exc).__name__}: {exc}",
        ) from exc


def _fixture_dir(provider_config: ProviderConfig, override: str | Path | None) -> Path:
    if override is not None:
        return Path(override)
    if provider_config.fixture_dir:
        return Path(provider_config.fixture_dir)
    return DEFAULT_FIXTURE_DIR


def _construct(
    kind: ProviderKind,
    name: str,
    provider_config: ProviderConfig,
    fixture_dir: str | Path | None,
) -> Provider:
    if kind is ProviderKind.FIXTURE:
        return FixtureProvider(_fixture_dir(provider_config, fixture_dir), name=name)
    module_name, class_name = _LAZY_PROVIDERS[kind]
    module = _import_optional(module_name)
    provider_class = getattr(module, class_name, None)
    if provider_class is None:
        raise ProviderError(
            "provider_not_implemented", f"{module_name} does not define {class_name}"
        )
    return provider_class(provider_config=provider_config, name=name)


def _coerce_kind(requested: ProviderKind | str) -> ProviderKind:
    if isinstance(requested, ProviderKind):
        return requested
    if isinstance(requested, str):
        try:
            return ProviderKind(requested.strip().upper())
        except ValueError as exc:
            raise ProviderError(
                "unknown_provider",
                f"{requested!r} is not a provider kind; expected one of "
                f"{[kind.value for kind in ProviderKind]}",
            ) from exc
    raise ProviderError(
        "unknown_provider",
        f"requested must be a ProviderKind or str, got {type(requested).__name__}",
    )


def _bob_credentials(kind: ProviderKind) -> tuple[bool, str]:
    """Whether Bob credentials exist, without ever reading their value into text."""
    credential_name = _BOB_CREDENTIAL_NAMES[kind]
    credential = get_credential(credential_name)
    if credential.value is not None:
        return True, f"credential {credential_name!r} found in {credential.source.value.lower()}"
    if os.environ.get(_BOB_ENV_VAR):
        return True, f"{_BOB_ENV_VAR} environment variable is set"
    return False, (
        f"no plain local credential {credential_name!r} in this folder and neither "
        f"{env_var_name(credential_name)} nor {_BOB_ENV_VAR} is set"
    )


def _check_requirements(
    config: AppConfig, kind: ProviderKind, *, allow_bob_shell: bool
) -> ProviderHealth:
    """Static eligibility, no network: credentials, endpoints, and policy flags."""
    if kind is ProviderKind.FIXTURE:
        return ProviderHealth(
            ok=True, code="ok", detail="fixture replay needs no credentials or network"
        )
    _, provider_config = _entry_for_kind(config, kind)

    if kind is ProviderKind.HTTP_INFERENCE:
        if not provider_config.endpoint:
            return ProviderHealth(
                ok=False,
                code="endpoint_not_configured",
                detail="ProviderConfig.endpoint is not set for the HTTP provider",
            )
        if not provider_config.model:
            return ProviderHealth(
                ok=False,
                code="model_not_configured",
                detail="ProviderConfig.model is not set for the HTTP provider",
            )
        return ProviderHealth(
            ok=True,
            code="ok",
            detail=f"endpoint={provider_config.endpoint} model={provider_config.model}",
        )

    if kind is ProviderKind.BOB_DIRECT:
        credentials_ok, detail = _bob_credentials(kind)
        if not credentials_ok:
            return ProviderHealth(ok=False, code="bob_credentials_unavailable", detail=detail)
        if not provider_config.endpoint or not provider_config.model:
            return ProviderHealth(
                ok=False,
                code="bob_endpoint_not_configured",
                detail=(
                    "ProviderConfig.endpoint and .model must be set from official Bob "
                    "documentation before Bob Direct can be used"
                ),
            )
        return ProviderHealth(ok=True, code="ok", detail=detail)

    if not config.data_policy.allow_bob_shell:
        return ProviderHealth(
            ok=False,
            code="bob_shell_not_allowed",
            detail="data_policy.allow_bob_shell is False; Bob Shell is disabled by policy",
        )
    if not allow_bob_shell:
        return ProviderHealth(
            ok=False,
            code="bob_shell_not_allowed",
            detail="--allow-bob-shell was not passed; Bob Shell is disabled",
        )
    credentials_ok, detail = _bob_credentials(kind)
    if not credentials_ok:
        return ProviderHealth(ok=False, code="bob_credentials_unavailable", detail=detail)
    return ProviderHealth(ok=True, code="ok", detail=detail)


def build_provider(
    config: AppConfig, *, name: str | None = None, fixture_dir: str | Path | None = None
) -> Provider:
    """Build the configured provider called ``name`` (default: the first in ``provider_order``).

    Raises :class:`ProviderError` when the entry is unknown, the kind is not
    implemented in this build, or Bob Shell is disabled by policy.
    """
    if name is None:
        if not config.provider_order:
            raise ProviderError(
                "provider_not_configured",
                "provider_order is empty; pass name= to choose a configured provider",
            )
        entry_name, provider_config = _entry_for_kind(config, config.provider_order[0])
    elif name in config.providers:
        entry_name, provider_config = name, config.providers[name]
    else:
        raise ProviderError(
            "provider_not_configured",
            f"config.providers has no entry {name!r}; configured: {sorted(config.providers)}",
        )

    if provider_config.kind is ProviderKind.BOB_SHELL and not config.data_policy.allow_bob_shell:
        raise ProviderError(
            "bob_shell_not_allowed",
            "data_policy.allow_bob_shell is False; Bob Shell is disabled by policy",
        )
    return _construct(provider_config.kind, entry_name, provider_config, fixture_dir)


def _try_kind(
    config: AppConfig,
    kind: ProviderKind,
    *,
    allow_bob_shell: bool,
    fixture_dir: str | Path | None,
) -> ProviderSelection:
    health = _check_requirements(config, kind, allow_bob_shell=allow_bob_shell)
    if not health.ok:
        raise ProviderError(health.code, health.detail)
    name, provider_config = _entry_for_kind(config, kind)
    provider = build_provider(config, name=name, fixture_dir=fixture_dir)

    if kind is ProviderKind.FIXTURE:
        detail = (
            f"provider {name!r} (FIXTURE) selected from provider_order: offline replay "
            "needs no credentials"
        )
        if isinstance(provider, FixtureProvider) and not provider.fixture_dir.is_dir():
            detail += (
                f"; fixture directory {provider.fixture_dir} does not exist yet, so extraction "
                "will report fixture_missing until fixtures are authored"
            )
        return ProviderSelection(provider=provider, kind=kind, detail=detail)

    live = provider.health(provider_config.timeout_s)
    if not live.ok:
        raise ProviderError(live.code, live.detail)
    return ProviderSelection(
        provider=provider,
        kind=kind,
        detail=f"provider {name!r} ({kind.value}) selected from provider_order: {live.detail}",
    )


def select_provider(
    config: AppConfig,
    *,
    requested: ProviderKind | str | None,
    allow_bob_shell: bool,
    fixture_dir: str | Path | None = None,
) -> ProviderSelection:
    """Choose a provider explicitly or by walking ``provider_order``.

    An explicit request is honoured or the call fails — it is never replaced by
    another provider. Without a request, the first healthy entry of
    ``provider_order`` is selected, or ``no_provider_available`` lists every
    rejection. Bob Shell additionally requires ``data_policy.allow_bob_shell``
    and ``allow_bob_shell=True``.
    """
    if requested is not None:
        kind = _coerce_kind(requested)
        health = _check_requirements(config, kind, allow_bob_shell=allow_bob_shell)
        if not health.ok:
            raise ProviderError(health.code, health.detail)
        name, _ = _entry_for_kind(config, kind)
        provider = build_provider(config, name=name, fixture_dir=fixture_dir)
        return ProviderSelection(
            provider=provider,
            kind=kind,
            detail=f"provider {name!r} ({kind.value}) was requested explicitly: {health.detail}",
        )

    if not config.provider_order:
        raise ProviderError(
            "no_provider_available",
            "config.provider_order is empty; request a provider explicitly or add one",
        )
    rejections: list[str] = []
    for kind in config.provider_order:
        try:
            return _try_kind(config, kind, allow_bob_shell=allow_bob_shell, fixture_dir=fixture_dir)
        except ProviderError as exc:
            rejections.append(f"{kind.value}: {exc.code}: {exc.detail}")
    raise ProviderError(
        "no_provider_available",
        "no provider in config.provider_order is usable: " + "; ".join(rejections),
    )
