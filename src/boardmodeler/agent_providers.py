"""IBM Bob is the only agent shipped by this edition.

The catalog is intentionally Bob-only in source, packaging and every UI surface.
Bob Shell consumes the inference key through the environment, never command arguments.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def only_provider() -> AgentProvider | None:
    """The one provider a restricted build accepts, or ``None`` when there is a choice.

    A build whose catalog holds a single entry offers no provider choice, so the surfaces
    that would offer one say whose build this is instead: the SETUP page says it accepts
    that provider's API only, and the window title carries the same fact (D-015).
    """
    return CATALOG[0] if len(CATALOG) == 1 else None
