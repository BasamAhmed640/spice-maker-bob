"""Text setup wizard shared by Setup.cmd and ``boardmodeler setup``.

All persistent choices come from the current edition's provider catalog and the
same config, credential, and LTspice modules used by model builds.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from boardmodeler import agent_providers
from boardmodeler.config import load_config, save_config
from boardmodeler.security.credentials import SecretSource, get_credential, set_credential
from boardmodeler.settings_summary import configured_provider
from boardmodeler.simulation.ltspice import locate, smoke_test
from boardmodeler.storage import app_root, data_dir, initialize, install_write_guard, local_path

LTSPICE_DOWNLOAD = (
    "https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html"
)


class SetupError(Exception):
    """A user-facing setup problem; never include a credential value."""


def _ask(label: str, current: str | None, *, yes: bool) -> str:
    if yes or not sys.stdin.isatty():
        return current or ""
    suffix = f" [{current}]" if current else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or current or ""


def _provider(wanted: str | None, config, *, yes: bool):
    entries = agent_providers.CATALOG
    if not entries:
        raise SetupError("This edition has no available agent provider.")
    if wanted:
        provider = agent_providers.by_id(wanted)
        if provider is None:
            raise SetupError(
                f"Provider {wanted!r} is unavailable. Choose one of: "
                + ", ".join(agent_providers.ids())
            )
        return provider
    configured, problem = configured_provider(config)
    if problem:
        if yes or not sys.stdin.isatty():
            raise SetupError(problem)
        print(problem)
        configured = None
    if len(entries) == 1:
        print(f"Provider: {entries[0].label}")
        return entries[0]
    print("Available providers:")
    for entry in entries:
        print(f"  {entry.id:16} {entry.label}")
    selected = _ask("Provider", configured.id if configured else None, yes=yes)
    provider = agent_providers.by_id(selected)
    if provider is None:
        raise SetupError("Choose a provider from the list above; setup was not saved.")
    return provider


def _internet(value: str | None, current: bool, *, yes: bool) -> bool:
    if value is not None:
        return value == "on"
    if yes or not sys.stdin.isatty():
        return current
    default = "Y/n" if current else "y/N"
    try:
        answer = input(f"Allow provider and vendor Internet access? [{default}] ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return current
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    raise SetupError("Answer y or n for Internet access.")


def _key(provider, key_env: str | None, *, yes: bool) -> str | None:
    if key_env is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key_env):
            raise SetupError("--key-env needs an environment variable name, not a key value.")
        value = os.environ.get(key_env, "")
        if not value.strip():
            raise SetupError(f"Environment variable {key_env} is empty or missing.")
        return value.strip()
    if get_credential(provider.credential).source is SecretSource.LOCAL_FILE:
        print(f"{provider.key_label}: using the saved key.")
        return None
    try:
        if sys.stdin.isatty():
            if yes:
                raise SetupError(
                    f"{provider.key_label} is missing. Set an environment variable and pass "
                    "its name with --key-env, or run Setup.cmd interactively."
                )
            value = getpass.getpass(f"{provider.key_label} (hidden): ")
        else:
            value = sys.stdin.readline()
    except EOFError, KeyboardInterrupt:
        value = ""
    if not value.strip():
        raise SetupError(f"{provider.key_label} was not entered; setup is unfinished.")
    return value.strip()


def _check_ltspice(value: str | None) -> str:
    if not value:
        raise SetupError(
            "LTspice has not been selected. Download it from "
            f"{LTSPICE_DOWNLOAD}, then run Setup.cmd again."
        )
    candidate = Path(value.strip().strip('"')).resolve()
    install = locate(candidate)
    if install is None:
        raise SetupError(f"LTspice executable does not exist: {candidate}")
    print("Running the existing LTspice RC smoke test...", flush=True)
    result = smoke_test(install.path, data_dir() / "temp" / "setup-smoke")
    if result.status != "pass":
        raise SetupError(f"LTspice smoke test failed: {result.detail}")
    print("LTspice smoke test passed.")
    return str(candidate)


def _model_folder(value: str | None) -> str:
    if not value:
        value = "models"
    folder = local_path(value.strip().strip('"'))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError("Could not create the model folder inside this app folder.") from exc
    return str(folder.relative_to(app_root()))


def _save(args: argparse.Namespace) -> int:
    config = load_config()
    print("Spice Maker settings")
    ltspice_value = _ask(
        "LTspice executable path", args.ltspice or config.ltspice.path, yes=args.yes
    )
    ltspice_path = _check_ltspice(ltspice_value)
    model_value = _ask(
        "Model folder inside this app",
        args.model_dir or config.default_model_dir or "models",
        yes=args.yes,
    )
    model_folder = _model_folder(model_value)
    provider = _provider(args.provider, config, yes=args.yes)
    access = _internet(args.internet, config.internet_access, yes=args.yes)
    value = _key(provider, args.key_env, yes=args.yes)
    from boardmodeler.security.network import _env_pins_off

    if value:
        # The former setup page kept the saved key after an inconclusive/rejected
        # connection check, so a user can correct provider access and retry.
        set_credential(provider.credential, value)
    config.ltspice.path = ltspice_path
    config.default_model_dir = model_folder
    config.agent_provider = provider.id
    config.internet_access = access
    config.setup_complete = True
    save_config(config)

    if value and access and not _env_pins_off():
        from boardmodeler.security.key_verification import verify_key

        print("Checking the selected provider key (up to 15 seconds)...", flush=True)
        result = verify_key(provider, value, model=config.agent_model)
        if result.status != "verified":
            raise SetupError(f"Key check {result.status}: {result.detail}")
        print("Provider key verified.")
    elif value:
        print("Internet access is off here; the key was saved without a connection check.")
    from boardmodeler.cli import _render_doctor_human, doctor_payload

    print("Checking setup with doctor...", flush=True)
    report = doctor_payload()
    print(_render_doctor_human(report))
    if not report["ok"]:
        config.setup_complete = False
        save_config(config)
        raise SetupError("Doctor could not confirm the LTspice setup.")
    print("Settings saved in this extracted copy.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure this Spice Maker copy")
    parser.add_argument("--ltspice", help="path pasted by the user; never searched for")
    parser.add_argument("--model-dir", help="model folder inside this extracted copy")
    parser.add_argument("--provider", help="provider ID from this edition's catalog")
    parser.add_argument("--internet", choices=("on", "off"))
    parser.add_argument(
        "--key-env", metavar="NAME", help="name of an environment variable holding the key"
    )
    parser.add_argument(
        "--yes", action="store_true", help="use the saved defaults without prompting"
    )
    args = parser.parse_args(argv)
    try:
        initialize()
        install_write_guard()
        return _save(args)
    except (OSError, SetupError, ValueError) as exc:
        print(f"Setup could not finish: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
