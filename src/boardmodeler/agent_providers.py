"""The agent providers this build accepts: raw API keys, no login flows.

The list is **data, not code**. A build ships the catalog it sells, and every consumer
(the setup page's provider row, the backend factory, ``doctor``) reads the catalog
instead of naming a provider itself. That is what makes the Bob-only build a one-entry
edit to :data:`CATALOG` rather than a fork of the code: with a single entry the setup
page shows no provider row and keeps the ``BOB API KEY`` label, and the backend factory
can only ever return Bob.

Every entry declares the transport it needs (``wire``), so an unsupported shape is
refused with a reason instead of being coerced:

``bob-shell``
    The Bob CLI (Bob Shell), the documented consumer of a Bob *Inference* API key: the
    key is passed to the child process through ``BOB_API_KEY``, never in argv. No login
    step, no endpoint in this file. ``https://bob.ibm.com/docs/shell/getting-started/install-and-setup``
``openai``
    ``POST <endpoint>/chat/completions``, ``Authorization: Bearer <key>`` — the shape
    OpenAI documents and several vendors implement. OpenCode Zen is the same shape on
    its own host; the models it serves from ``/responses`` and ``/messages`` are not
    reachable through this wire, and the MODEL row in SETUP is where that choice lives.
``anthropic``
    ``POST <endpoint>/messages`` with ``x-api-key`` and ``anthropic-version``.
``google``
    ``POST <endpoint>/models/<model>:generateContent`` with ``x-goog-api-key``.

Endpoints and default model ids are the vendors' own documented values and are never
guessed (D-005); each entry carries the documentation URL it came from. A model id can
be overridden per machine in SETUP or with ``--model``, because these strings do drift.
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
WIRES: tuple[str, ...] = ("bob-shell", "openai", "anthropic", "google")


@dataclass(frozen=True)
class AgentProvider:
    """One accepted way to reach an agent, and how its key is stored.

    ``credential`` is the name used with :mod:`boardmodeler.security.credentials`
    (keyring entry ``provider:<credential>:api_key``, environment fallback
    ``BOARDMODELER_<CREDENTIAL>_API_KEY``); ``env_aliases`` are additional plain
    environment variables the same key is read from, for people who already export
    the vendor's own variable. ``key_label`` and ``key_hint`` are the setup page's
    label and hint, so a Bob-only build keeps exactly today's wording.
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
    #: On the ``openai`` wire, whether the entry's model is a reasoning model: it
    #: rejects ``temperature`` and takes its output budget as
    #: ``max_completion_tokens`` rather than ``max_tokens``. OpenAI's own reasoning
    #: models do; most OpenAI-compatible vendors still take the classic parameters,
    #: so this is per entry rather than per wire.
    reasoning: bool = False

    @property
    def uses_cli(self) -> bool:
        """Whether the key is consumed by a local CLI process rather than HTTP."""
        return self.wire == "bob-shell"

    @property
    def model_editable(self) -> bool:
        """Whether this provider takes a model id from this application at all."""
        return not self.uses_cli


#: The provider this build accepts. It ships IBM Bob only: with one entry the setup page
#: has no provider row and says whose build this is, the backend factory can only return
#: Bob, and a config naming any other provider falls back to Bob. The general build adds
#: the mainstream vendors - and OpenCode Zen / Go - to this tuple (D-015).
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
    """The one provider a restricted build accepts, or ``None`` when there is a choice.

    A build whose catalog holds a single entry offers no provider choice, so the surfaces
    that would offer one say whose build this is instead: the SETUP page says it accepts
    that provider's API only, and the window title carries the same fact (D-015).
    """
    return CATALOG[0] if len(CATALOG) == 1 else None
