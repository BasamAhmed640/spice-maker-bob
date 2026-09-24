"""Disabled legacy Bob extraction adapters.

The supported Bob integration is :class:`boardmodeler.authoring.backends.BobShellBackend`.
Bob has no documented direct HTTP endpoint for this application, and legacy Shell
extraction accepted caller-controlled commands and tools. Both adapters remain as
small blocked shims so old configuration files fail clearly and safely.
"""

from __future__ import annotations

from collections.abc import Mapping

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
)

__all__ = ["BOB_API_KEY_ENV", "BobDirectProvider", "BobShellProvider"]

BOB_API_KEY_ENV = "BOB_API_KEY"
_DIRECT_CODE = "bob_direct_unsupported"
_DIRECT_DETAIL = "Bob Direct extraction is unavailable; use the tool-free Bob Shell author backend"
_SHELL_CODE = "legacy_bob_shell_disabled"
_SHELL_DETAIL = "Legacy Bob extraction can enable tools; use the tool-free Bob Shell author backend"


class BobDirectProvider:
    def __init__(
        self,
        *,
        provider_config: ProviderConfig,
        name: str = "bob_direct",
        **_legacy_options: object,
    ) -> None:
        self.provider_config = provider_config
        self.name = name

    def identity(self, *, usage: Mapping[str, float] | None = None) -> ProviderIdentity:
        return ProviderIdentity(
            provider=self.name,
            kind=ProviderKind.BOB_DIRECT,
            model=self.provider_config.model,
            endpoint=None,
            usage_units="tokens",
            usage=dict(usage or {}),
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=False,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="tokens",
            notes=_DIRECT_DETAIL,
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        return ProviderHealth(ok=False, code=_DIRECT_CODE, detail=_DIRECT_DETAIL)

    def extract(
        self, request: ExtractionRequest, cancel: object = None
    ) -> ExtractionResponse:
        raise ProviderError(_DIRECT_CODE, _DIRECT_DETAIL)


class BobShellProvider:
    def __init__(
        self,
        *,
        provider_config: ProviderConfig,
        name: str = "bob_shell",
        **_legacy_options: object,
    ) -> None:
        self.provider_config = provider_config
        self.name = name

    def identity(self, *, turns: float = 0.0, exit_code: str | None = None) -> ProviderIdentity:
        return ProviderIdentity(
            provider=self.name,
            kind=ProviderKind.BOB_SHELL,
            model=self.provider_config.model,
            endpoint=None,
            usage_units="turns",
            usage={"turns": float(turns)},
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=False,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="turns",
            notes=_SHELL_DETAIL,
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        return ProviderHealth(ok=False, code=_SHELL_CODE, detail=_SHELL_DETAIL)

    def extract(
        self, request: ExtractionRequest, cancel: object = None
    ) -> ExtractionResponse:
        raise ProviderError(_SHELL_CODE, _SHELL_DETAIL)
