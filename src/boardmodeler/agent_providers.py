"""The agent providers this build accepts: raw API keys, no login flows.

The list is **data, not code**. A build ships the catalog it sells, and every consumer
(the setup page's provider row, the backend factory, ``doctor``) reads the catalog
instead of naming a provider itself: a catalog with a single entry shows no provider row
and keeps that provider's own key label, and the backend factory can only return it.

Every entry declares the transport it needs (``wire``), and this build has exactly one:
the documented Bob Shell interface. A catalog entry naming any other transport is refused
by the backend factory (``wire_unsupported``) rather than coerced onto this one.

``bob-shell``
    The Bob CLI (Bob Shell), the documented consumer of a Bob *Inference* API key: the
    key is passed to the child process through ``BOB_API_KEY``, never in argv. No login
    step, no endpoint in this file. ``https://bob.ibm.com/docs/shell/getting-started/install-and-setup``

Endpoints and default model ids are the vendors' own documented values and are never
guessed (D-005); each entry carries the documentation URL it came from, and where a
vendor takes a model id it can be overridden per machine in SETUP rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CATALOG",
    "DEFAULT_PROVIDER_ID",
    "AgentProvider",
    "by_id",
    "default_provider",
    "ids",
    "only_provider",
    "require",
]

#: Wires this build knows how to speak. A catalog entry naming anything else is a
#: build defect, and ``by_id``/``require`` say so instead of picking a fallback.
WIRES: tuple[str, ...] = ("bob-shell",)


@dataclass(frozen=True)
class AgentProvider:
    """One accepted way to reach an agent, and how its key is stored.

    ``credential`` is the name used with :mod:`boardmodeler.security.credentials`
    (keyring entry ``provider:<credential>:api_key``, environment fallback
    ``BOARDMODELER_<CREDENTIAL>_API_KEY``); ``env_aliases`` are additional plain
    environment variables the same key is read from, for people who already export
    the vendor's own variable. ``key_label`` and ``key_hint`` are the setup page's
    label and hint for that entry's key.
    """

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
        """Whether the key is consumed by a local CLI process rather than sent to an endpoint."""
        return self.wire == "bob-shell"

    @property
    def model_editable(self) -> bool:
        """Whether this provider takes a model id from this application at all."""
        return not self.uses_cli


#: The provider this build accepts. With one entry the setup page has no provider row,
#: the backend factory can only return that provider, and a config naming any other
#: provider falls back to it.
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

#: The provider a build expects when the user has not chosen one. Bob, always.
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
        raise ValueError(
            f"provider {provider_id!r} is not accepted by this build; accepted: {list(ids())}"
        )
    return provider


def default_provider() -> AgentProvider:
    """The default entry: :data:`DEFAULT_PROVIDER_ID`, or the only entry there is."""
    provider = by_id(DEFAULT_PROVIDER_ID)
    if provider is not None:
        return provider
    if not CATALOG:  # pragma: no cover - a build with no provider cannot run anything
        raise ValueError("this build has an empty provider catalog")
    return CATALOG[0]


def only_provider() -> AgentProvider | None:
    """The one provider a catalog with a single entry holds, or ``None`` when there is a choice.

    A build whose catalog holds a single entry offers no provider choice, so the surfaces
    that would offer one state that provider instead.
    """
    return CATALOG[0] if len(CATALOG) == 1 else None
