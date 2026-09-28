"""The persisted settings as data, shared by the text setup and CLI report."""

from __future__ import annotations

import os
from pathlib import Path

from boardmodeler import agent_providers
from boardmodeler import config as _config
from boardmodeler.agent_providers import AgentProvider
from boardmodeler.config import AppConfig
from boardmodeler.storage import app_root, library_dir, portable

__all__ = [
    "configured_provider",
    "credential_file_label",
    "describe_settings",
    "ltspice_user_lib",
]


def ltspice_user_lib(home: Path | None = None) -> Path:
    """The per-user LTspice library (never the installation directory)."""
    if portable():
        return library_dir()
    base = home if home is not None else Path.home()
    return base / "AppData" / "Local" / "LTspice" / "lib"


def credential_file_label() -> str:
    """Where the API key is saved, relative to this extracted folder when it is inside it.

    Resolved, never hard-coded: the Bob edition writes a different file name, and the
    setup must not name a file that this build does not use.
    """
    from boardmodeler.security.credentials import credential_path

    path = credential_path()
    try:
        return str(path.relative_to(app_root())).replace(os.sep, "/")
    except ValueError:  # pragma: no cover - the credential file is always inside the copy
        return str(path)


def configured_provider(config: AppConfig) -> tuple[AgentProvider | None, str]:
    """``(provider, reason)`` for ``config.agent_provider``; never another provider.

    An id this build does not accept comes back as ``None`` plus the same
    ``api_provider_unavailable`` text
    :func:`boardmodeler.authoring.api_backend.build_api_backend` refuses with, so
    setup and the engine cannot disagree about the refusal. An
    empty setting means this build's default provider.
    """
    wanted = str(config.agent_provider or "").strip()
    if not wanted:
        return agent_providers.default_provider(), ""
    provider = agent_providers.by_id(wanted)
    if provider is not None:
        return provider, ""
    return None, (
        "api_provider_unavailable: this edition accepts IBM Bob only; "
        "run boardmodeler setup --provider bob"
    )


def describe_settings(config: AppConfig) -> dict[str, object]:
    """The persisted settings as data, for ``boardmodeler setup --json`` and tests."""
    from boardmodeler.security.credentials import describe_credential

    provider, reason = configured_provider(config)
    shown = provider or agent_providers.default_provider()
    return {
        # Through the module, not a name bound at import time: a test or a caller that
        # redirects ``boardmodeler.config.config_path`` must redirect this report too,
        # and an early binding silently ignored the redirect.
        "config_path": str(_config.config_path()),
        "ltspice_path": config.ltspice.path,
        "model_dir": config.default_model_dir,
        "internet_access": config.internet_access,
        "ltspice_user_lib": str(ltspice_user_lib()),
        "agent_provider": provider.id if provider else None,
        "agent_provider_accepted": provider is not None,
        "agent_provider_problem": reason,
        "agent_model": None,
        "agent_api_key": describe_credential(shown.credential),
        "accepted_providers": list(agent_providers.ids()),
    }
