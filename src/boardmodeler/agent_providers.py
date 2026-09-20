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

from collections.abc import Mapping
from dataclasses import dataclass, field

from boardmodeler.build_flavor import BOB_ONLY

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
    #: Extra fields merged into the request body for this entry, in the vendor's own
    #: documented spelling. This exists because a vendor knob can decide whether the
    #: task is possible at all: DeepSeek's ``{"thinking": {"type": "enabled"},
    #: "reasoning_effort": "low"}`` is the setting measured to produce a model the
    #: harness can judge (0 PASS with thinking off, 4 PASS / 0 FAIL at low effort), and
    #: the same switch will occasionally spend the whole output budget reasoning.
    extra_body: Mapping[str, object] = field(default_factory=dict)
    #: Used *instead of* :attr:`extra_body` for one re-ask inside the same turn when the
    #: reply carries no text because the model stopped at its output budget: the entry's
    #: own documented way of making it answer at all (DeepSeek: thinking off, ~10 s).
    #: Empty means the entry has no such second setting and the turn fails honestly.
    retry_body: Mapping[str, object] = field(default_factory=dict)

    @property
    def uses_cli(self) -> bool:
        """Whether the key is consumed by a local CLI process rather than HTTP."""
        return self.wire == "bob-shell"

    @property
    def model_editable(self) -> bool:
        """Whether this provider takes a model id from this application at all."""
        return not self.uses_cli


#: The providers a build accepts, default first. Trim this tuple for a restricted
#: build; nothing else in the code names a provider.
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
    AgentProvider(
        id="deepseek",
        label="DeepSeek",
        wire="openai",
        credential="deepseek",
        key_label="DEEPSEEK API KEY",
        key_hint="platform.deepseek.com → API keys  ·  stored in the Windows credential store",
        docs="https://api-docs.deepseek.com/",
        endpoint="https://api.deepseek.com",
        model="deepseek-flash",
        env_aliases=("DEEPSEEK_API_KEY",),
        # DeepSeek's own "Invoke The Chat API" example documents this switch, and the
        # measured settings differ: thinking off answers in ~10 s but the model it writes
        # reaches 0 PASS, thinking on at low effort reaches 4 PASS / 0 FAIL in three
        # turns — and once in a while spends the whole budget reasoning anyway, which is
        # what ``retry_body`` is for (D-015).
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        retry_body={"thinking": {"type": "disabled"}},
    ),
    AgentProvider(
        id="openai",
        label="OpenAI",
        wire="openai",
        credential="openai",
        key_label="OPENAI API KEY",
        key_hint="platform.openai.com → API keys  ·  stored in the Windows credential store",
        docs="https://developers.openai.com/api/docs/guides/text",
        endpoint="https://api.openai.com/v1",
        model="gpt-6-astra",
        env_aliases=("OPENAI_API_KEY",),
        reasoning=True,
    ),
    AgentProvider(
        id="anthropic",
        label="Anthropic",
        wire="anthropic",
        credential="anthropic",
        key_label="ANTHROPIC API KEY",
        key_hint="console.anthropic.com → API keys  ·  stored in the Windows credential store",
        docs="https://platform.claude.com/docs/en/get-started",
        endpoint="https://api.anthropic.com/v1",
        model="claude-opus-5",
        env_aliases=("ANTHROPIC_API_KEY",),
    ),
    AgentProvider(
        id="google",
        label="Google Gemini",
        wire="google",
        credential="google",
        key_label="GEMINI API KEY",
        key_hint="aistudio.google.com → API keys  ·  stored in the Windows credential store",
        docs="https://ai.google.dev/gemini-api/docs/text-generation",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-3.8-flash",
        env_aliases=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
    AgentProvider(
        id="openrouter",
        label="OpenRouter",
        wire="openai",
        credential="openrouter",
        key_label="OPENROUTER API KEY",
        key_hint="openrouter.ai → keys  ·  stored in the Windows credential store",
        docs="https://openrouter.ai/docs/quickstart",
        endpoint="https://openrouter.ai/api/v1",
        model="~openai/gpt-sol-latest",
        env_aliases=("OPENROUTER_API_KEY",),
    ),
    AgentProvider(
        id="xai",
        label="xAI",
        wire="openai",
        credential="xai",
        key_label="XAI API KEY",
        key_hint="console.x.ai → API keys  ·  stored in the Windows credential store",
        docs="https://docs.x.ai/developers/models",
        endpoint="https://api.x.ai/v1",
        model="grok-4.6",
        env_aliases=("XAI_API_KEY",),
    ),
    AgentProvider(
        id="groq",
        label="Groq",
        wire="openai",
        credential="groq",
        key_label="GROQ API KEY",
        key_hint="console.groq.com → API keys  ·  stored in the Windows credential store",
        docs="https://console.groq.com/docs/api-reference",
        endpoint="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        env_aliases=("GROQ_API_KEY",),
    ),
    AgentProvider(
        id="opencode",
        label="OpenCode Zen / Go",
        wire="openai",
        credential="opencode",
        key_label="OPENCODE API KEY",
        key_hint="opencode.ai/auth → API key (Zen pay-as-you-go, or the Go subscription)",
        docs="https://opencode.ai/docs/zen",
        endpoint="https://opencode.ai/zen/v1",
        model="deepseek-v4-flash",
        env_aliases=("OPENCODE_API_KEY",),
    ),
    AgentProvider(
        id="mistral",
        label="Mistral",
        wire="openai",
        credential="mistral",
        key_label="MISTRAL API KEY",
        key_hint="console.mistral.ai → API keys  ·  stored in the Windows credential store",
        docs="https://docs.mistral.ai/getting-started/quickstarts/developer/first-api-request",
        endpoint="https://api.mistral.ai/v1",
        model="mistral-large-latest",
        env_aliases=("MISTRAL_API_KEY",),
    ),
)

if BOB_ONLY:
    CATALOG = tuple(entry for entry in CATALOG if entry.id == "bob")


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
