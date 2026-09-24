"""Legacy Bob provider routes must not send credentials or run tools."""

from __future__ import annotations

import pytest

from boardmodeler.config import AppConfig, ProviderConfig
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.providers.base import ProviderError
from boardmodeler.providers.bob import BobDirectProvider, BobShellProvider
from boardmodeler.providers.registry import build_provider, select_provider


def test_bob_direct_never_sends_to_a_configured_endpoint() -> None:
    provider = BobDirectProvider(
        provider_config=ProviderConfig(
            kind=ProviderKind.BOB_DIRECT,
            endpoint="https://attacker.example/v1/chat/completions",
            model="arbitrary",
        ),
        transport=lambda _request: pytest.fail("Bob Direct sent a credential"),
    )
    assert provider.health(1.0).code == "bob_direct_unsupported"
    with pytest.raises(ProviderError, match="bob_direct_unsupported"):
        provider.extract(None)  # type: ignore[arg-type]


def test_legacy_bob_shell_never_runs_a_configured_command(tmp_path) -> None:
    provider = BobShellProvider(
        provider_config=ProviderConfig(kind=ProviderKind.BOB_SHELL),
        allow_bob_shell=True,
        argv_template=["bob", "run", "--allow-tools"],
        tools=["execute"],
        workdir=tmp_path,
        runner=lambda *args, **kwargs: pytest.fail("legacy Bob command ran"),
    )
    assert provider.health(1.0).code == "legacy_bob_shell_disabled"
    with pytest.raises(ProviderError, match="legacy_bob_shell_disabled"):
        provider.extract(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", [ProviderKind.BOB_DIRECT, ProviderKind.BOB_SHELL])
def test_registry_blocks_legacy_bob_provider(kind) -> None:
    config = AppConfig()
    config.providers[kind.value.lower()] = ProviderConfig(kind=kind)
    with pytest.raises(ProviderError, match="legacy_provider_disabled"):
        build_provider(config, name=kind.value.lower())
    with pytest.raises(ProviderError):
        select_provider(config, requested=kind, allow_bob_shell=True)
