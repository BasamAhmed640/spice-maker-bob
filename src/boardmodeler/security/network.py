"""The one switch that governs every request this product makes.

SETUP holds exactly one checkbox for network access, and this module is how the rest
of the product *asks* whether it is on. The switch governs two different things — the
agent provider's API, which authors a model, and the part vendor's own site, which is
read for supporting material — and it governs them together so a user cannot end up
with "the web is off" plus a build that quietly dials out anyway.

Three properties this module exists to keep:

* **One reader.** ``AppConfig.internet_access`` is the only stored setting; every call
  site asks :func:`internet_allowed` or :func:`require_network` instead of reading the
  config itself, so the mapping from switch to behaviour exists once.
* **Refuse before building the request.** :func:`require_network` raises
  :class:`NetworkRefused` at the *start* of a stage, so the caller is told immediately
  rather than after minutes of work, and so no partially-built request can be sent.
* **The environment can always pin it off.** ``BOARDMODELER_NO_NETWORK`` (any truthy
  value) forces the answer to ``False`` whatever the config file says. That is what
  makes a hermetic test — and a cautious user — possible without editing the user's
  settings, and it wins over the file in both directions of the switch.

Nothing here is a security boundary against a hostile config: it is the honest
implementation of one user-visible control. The containment checks that stop a
*malformed* destination live in :mod:`boardmodeler.agent_providers` and
:mod:`boardmodeler.security.subprocess_guard`.
"""

from __future__ import annotations

import os

__all__ = [
    "NETWORK_ENV_VAR",
    "NetworkRefused",
    "internet_allowed",
    "refusal_detail",
    "require_network",
]

#: The environment variable that pins network access off regardless of the config.
NETWORK_ENV_VAR = "BOARDMODELER_NO_NETWORK"

#: Spellings that mean "off"; anything else non-empty counts as truthy, so setting the
#: variable to a value nobody would call false can never silently enable egress.
_FALSY = frozenset({"0", "false", "no", "off", "n", "f"})

#: Where the switch lives, named so a refusal tells the user where to change it.
SETUP_SWITCH = "the INTERNET ACCESS switch in SETUP"


def _env_pins_off() -> bool:
    """True when the environment forces network access off, whatever the file says."""
    raw = os.environ.get(NETWORK_ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    return bool(value) and value not in _FALSY


def refusal_detail(stage: str) -> str:
    """Why ``stage`` was refused, in the words the user sees.

    The two things a person needs are here: which control to change (SETUP's
    INTERNET ACCESS switch) and which override can also be holding it shut
    (``BOARDMODELER_NO_NETWORK``). The reason is built from literals only, so it cannot
    carry a datasheet path, a key or a URL into a log.
    """
    return (
        f"internet_access_off: {SETUP_SWITCH} is off, {NETWORK_ENV_VAR} is set, or "
        f"settings could not be read, so {stage} was refused before any request was made; "
        f"turn INTERNET ACCESS on in SETUP, unset {NETWORK_ENV_VAR}, and repair any "
        "invalid settings to allow it"
    )


class NetworkRefused(RuntimeError):
    """A stage that would have sent a request, refused before the request existed.

    ``code`` is stable so a caller can branch on it without matching prose, and
    ``detail`` names both the SETUP switch and the environment override.
    """

    code = "internet_access_off"

    def __init__(self, stage: str, detail: str | None = None) -> None:
        self.stage = stage
        self.detail = detail or refusal_detail(stage)
        super().__init__(f"{self.code}: {self.detail}")


def internet_allowed() -> bool:
    """``True`` only when the user's switch is on *and* the environment has not pinned it off.

    The environment is consulted first, so a pinned-off session answers ``False`` even
    when the config file cannot be read at all. A broken config fails closed so a
    saved off preference cannot silently turn into permission to send a request.
    """
    if _env_pins_off():
        return False
    try:
        from boardmodeler.config import load_config
    except ImportError:  # pragma: no cover - the package is always importable together
        return False
    try:
        return bool(load_config().internet_access)
    except Exception:
        return False


def require_network(stage: str) -> None:
    """Raise :class:`NetworkRefused` unless network access is on, before any request.

    ``stage`` names the piece of work that was about to reach the network ("authoring",
    "the supporting-material search") and is what the user is told was refused.
    """
    if not internet_allowed():
        raise NetworkRefused(stage)
