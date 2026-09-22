"""IBM Bob is the only agent shipped by this edition.

The catalog is intentionally Bob-only in source, packaging and every UI surface.
Bob Shell consumes the inference key through the environment, never command arguments.

Bob is a *CLI* provider (``wire="bob-shell"``): the entry declares no HTTP endpoint,
and :func:`endpoint_is_vendor` therefore refuses every URL for it. Nothing here may
invent one — an inferred destination would be exactly the guess D-005 forbids.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

WIRES = ("bob-shell",)


@dataclass(frozen=True)
class AgentProvider:
    id: str
    label: str
    wire: str
    credential: str
    key_label: str
    key_hint: str
    docs: str
    endpoint: str | None = None
    model: str | None = None
    env_aliases: tuple[str, ...] = ()

    @property
    def uses_cli(self) -> bool:
        return True

    @property
    def model_editable(self) -> bool:
        return False


__all__ = [
    "CATALOG",
    "DEFAULT_PROVIDER_ID",
    "AgentProvider",
    "by_id",
    "default_provider",
    "endpoint_is_vendor",
    "ids",
    "only_provider",
    "require",
    "vendor_host",
]


CATALOG: tuple[AgentProvider, ...] = (
    AgentProvider(
        id="bob",
        label="IBM Bob",
        wire="bob-shell",
        credential="bob_shell",
        key_label="BOB API KEY",
        key_hint="bob.ibm.com → API keys → Scope = Inference  ·  used by the Bob CLI",
        docs="https://bob.ibm.com/docs/shell/getting-started/install-and-setup",
        env_aliases=("BOB_API_KEY",),
    ),
)

DEFAULT_PROVIDER_ID = "bob"


def ids() -> tuple[str, ...]:
    """The ids this build accepts, in catalog order."""
    return tuple(provider.id for provider in CATALOG)


def by_id(provider_id: str | None) -> AgentProvider | None:
    """The catalog entry with ``provider_id``, or ``None`` when this build has none."""
    if not provider_id:
        return None
    wanted = str(provider_id).strip().lower()
    for provider in CATALOG:
        if provider.id == wanted:
            return provider
    return None


def require(provider_id: str | None) -> AgentProvider:
    """As :func:`by_id`, but raise for a provider this build does not accept."""
    provider = by_id(provider_id)
    if provider is None:
        raise ValueError("This edition accepts IBM Bob only.")
    return provider


def default_provider() -> AgentProvider:
    """The default entry: :data:`DEFAULT_PROVIDER_ID`, or the only entry there is."""
    provider = by_id(DEFAULT_PROVIDER_ID)
    if provider is not None:
        return provider
    if not CATALOG:  # pragma: no cover - a build with no provider cannot run anything
        raise ValueError("this build has an empty provider catalog")
    return CATALOG[0]


def vendor_host(url: str | None) -> str | None:
    """The host a URL (or bare host) names, normalized for comparison.

    Comparison never cares about scheme, port, case, a trailing root dot or
    userinfo: only the host decides. A value this function cannot parse yields
    ``None``, and a caller that needs a decision must refuse rather than treat
    ``None`` as a match.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.hostname is None:  # a bare ``api.example.com/v1`` has no scheme to split
        parts = urlsplit(f"//{text}")
    host = parts.hostname
    if host is None:
        return None
    return host.strip().strip(".").lower() or None


def _is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and every loopback address: never internet egress."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped if mapped is not None else address).is_loopback)


def endpoint_is_vendor(provider: str | None, url: str) -> tuple[bool, str]:
    """``(allowed, reason)`` for one inference destination.

    This is the single source of truth for inference egress: the host a request
    would go to must be exactly the host the selected :data:`CATALOG` entry
    declares in its documented ``endpoint``. Scheme and port are ignored; the
    host is compared as a whole, so ``https://api.deepseek.com.evil.test/v1``
    and ``https://evilapi.deepseek.com/v1`` both fail against
    ``api.deepseek.com`` (no suffix, substring or registrable-domain match — a
    lookalike domain is a different domain).

    A provider id outside the catalog declares no vendor host at all, so the
    only destination it may reach is a loopback one (a local fixture or test
    server, which is not internet egress); a public destination is refused
    rather than guessed. This edition's one entry is a CLI provider (``bob``)
    that declares no HTTP endpoint, so any URL for it is refused: a CLI has no
    wire to send it to, and inventing a host would be the guess D-005 forbids.
    The reason names the provider, the expected host and the documentation it
    came from, so a refusal is reported instead of retried.
    """
    host = vendor_host(url)
    if host is None:
        return False, f"endpoint_host_missing: {url!r} names no host to match"
    entry = by_id(provider)
    if entry is None:
        if _is_loopback_host(host):
            return True, f"local_endpoint: {host!r} is loopback, not internet egress"
        named = "<none>" if not provider else str(provider)
        return False, (
            f"provider_not_in_catalog: provider {named!r} is not a provider this build "
            f"accepts ({list(ids())}), so no vendor host is declared; refusing the internet "
            f"destination {host!r}"
        )
    if not entry.endpoint:
        return False, (
            f"provider_has_no_http_endpoint: provider {entry.id!r} uses the {entry.wire!r} wire "
            f"and declares no HTTP endpoint (documentation: {entry.docs}); refusing {host!r}"
        )
    expected = vendor_host(entry.endpoint)
    if expected is None:  # pragma: no cover - a catalog entry with an unusable endpoint
        return False, (
            f"provider_endpoint_unusable: provider {entry.id!r} declares an endpoint with no "
            f"host ({entry.endpoint!r}); refusing {host!r}"
        )
    if host != expected:
        return False, (
            f"endpoint_not_vendor: provider {entry.id!r} declares host {expected!r} "
            f"(documentation: {entry.docs}); refusing {host!r}"
        )
    return True, f"vendor_endpoint: {host!r} is the host provider {entry.id!r} declares"


def only_provider() -> AgentProvider | None:
    """The one provider a restricted build accepts, or ``None`` when there is a choice.

    A build whose catalog holds a single entry offers no provider choice, so the surfaces
    that would offer one say whose build this is instead: the SETUP page says it accepts
    that provider's API only, and the window title carries the same fact (D-015).
    """
    return CATALOG[0] if len(CATALOG) == 1 else None
