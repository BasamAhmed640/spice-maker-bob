"""Application configuration in this extracted copy's data/config.json.

Precedence, highest first:

1. explicit constructor argument (CLI switch, GUI field)
2. ``BOARDMODELER_CONFIG`` (path to the config file itself)
3. the config file
4. built-in defaults

Nothing here writes secrets: credentials live in this copy's local credential file or
the ``BOARDMODELER_<NAME>_API_KEY`` environment variable (see ``security.credentials``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from boardmodeler.domain import SCHEMA_VERSION
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.security.policy import DataPolicy
from boardmodeler.storage import data_dir, local_path, portable, state_file

__all__ = [
    "AppConfig",
    "LtspiceConfig",
    "ProviderConfig",
    "config_dir",
    "config_path",
    "load_config",
    "save_config",
]

CONFIG_ENV_VAR = "BOARDMODELER_CONFIG"
APP_DIR_NAME = "BoardModeler"


class LtspiceConfig(BaseModel):
    """Where LTspice is and how it is invoked.

    ``path=None`` means SETUP has not chosen an executable yet. It is never taken
    as an instruction to search the machine: ``simulation.ltspice.locate`` resolves
    only what is written here. Users choose the path in SETUP.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    timeout_s: float = 120.0
    extra_switches: list[str] = Field(default_factory=list)
    search_paths: list[str] = Field(default_factory=list)
    lib_dir: str | None = None
    """Read-only directory the simulator resolves ``.include`` names against."""


class ProviderConfig(BaseModel):
    """Configuration for one extraction provider.

    ``endpoint`` and ``model`` must be strings observed in vendor documentation;
    they are never guessed (see DECISIONS D-005).
    """

    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind = ProviderKind.FIXTURE
    fixture_dir: str | None = None
    endpoint: str | None = None
    model: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    timeout_s: float = 60.0
    retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=4096, ge=1)
    temperature: float = 0.0


class AppConfig(BaseModel):
    """Top-level application configuration."""

    model_config = ConfigDict(extra="forbid")

    config_version: int = SCHEMA_VERSION
    ltspice: LtspiceConfig = Field(default_factory=LtspiceConfig)
    provider_order: list[ProviderKind] = Field(
        default_factory=lambda: [
            ProviderKind.FIXTURE,
            ProviderKind.BOB_DIRECT,
            ProviderKind.HTTP_INFERENCE,
        ]
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "fixture": ProviderConfig(kind=ProviderKind.FIXTURE),
            "http_inference": ProviderConfig(kind=ProviderKind.HTTP_INFERENCE),
            "bob_direct": ProviderConfig(kind=ProviderKind.BOB_DIRECT),
            "bob_shell": ProviderConfig(kind=ProviderKind.BOB_SHELL),
        }
    )
    data_policy: DataPolicy = Field(default_factory=DataPolicy)
    max_repair_iterations: int = Field(default=3, ge=0, le=10)
    #: Where finished models are saved by default (setup page).
    default_model_dir: str | None = None
    #: Agent provider id from this build's catalog (see ``boardmodeler.agent_providers``).
    #: ``None`` means the build's default provider, so no provider name is baked in here.
    agent_provider: str | None = None
    #: Model id for providers that take one from this application; ``None`` uses the
    #: provider's documented default.
    agent_model: str | None = None
    #: Output-token budget for one HTTP authoring reply; ``None`` uses
    #: ``authoring.api_backend``'s default. Reasoning-class models spend part of this
    #: budget before they write any file text, which is why it is generous and settable.
    agent_max_tokens: int | None = Field(default=None, ge=1)
    #: Search the web for supporting material while a model is being made (setup page).
    web_reinforcement: bool = True
    default_project_dir: str | None = None
    log_level: str = "INFO"
    setup_complete: bool = False
    full_verification: bool = True


def config_dir() -> Path:
    """Settings for this copy only; never read a previous user-profile install."""
    return data_dir()


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    if override and not portable():
        return Path(override)
    return state_file("config.json")


def load_config(path: Path | None = None) -> AppConfig:
    """Load the config, returning defaults when the file does not exist.

    A malformed config is an error, not silently replaced by defaults — running
    with different settings than the user wrote would be worse than failing.
    """
    target = local_path(path or config_path()) if portable() else (path or config_path())
    if not target.exists():
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # The type is preserved: a broken file stays an error, never defaults.
        raise json.JSONDecodeError(
            f"{target} is not valid JSON: {exc.msg}", exc.doc, exc.pos
        ) from exc
    return AppConfig.model_validate(raw)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write the config atomically (temp file + replace) and return its path."""
    target = local_path(path or config_path()) if portable() else (path or config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = config.model_dump(mode="json", exclude_defaults=True)
    payload["config_version"] = config.config_version
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target
