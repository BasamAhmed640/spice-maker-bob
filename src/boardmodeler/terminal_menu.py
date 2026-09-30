"""Small text front end for the existing model and setup commands."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from boardmodeler.config import load_config
from boardmodeler.storage import local_path, model_dir

Command = Callable[[list[str]], int]
Reader = Callable[[str], str]


def _read(reader: Reader, prompt: str) -> str | None:
    try:
        return reader(prompt)
    except EOFError, KeyboardInterrupt:
        print()
        return None


def _unquote_path(value: str) -> str:
    """Accept a PDF or folder dragged into a Windows console."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _model_home() -> Path | None:
    try:
        return model_dir(load_config().default_model_dir)
    except (OSError, ValueError) as exc:
        print(f"Settings could not be read: {exc}")
        print("Choose Settings to repair them.")
        return None


def _new_model_path(base: Path, name: str) -> Path:
    path = base / name
    suffix = 2
    while path.exists():
        path = base / f"{name}-{suffix}"
        suffix += 1
    return path


def _build(reader: Reader, command: Command) -> int:
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        print(f"Settings could not be read: {exc}")
        print("Choose Settings to repair them.")
        return 1
    if not config.setup_complete:
        print("Setup is unfinished. Choose Settings, or run Setup.cmd, before building a model.")
        return 1

    part = _read(reader, "Part number: ")
    if part is None:
        return 0
    part = part.strip()
    if not part or not any(character.isalnum() for character in part):
        print("Enter a part number.")
        return 1
    supplied = _read(reader, "Datasheet PDF path: ")
    if supplied is None:
        return 0
    try:
        datasheet = Path(_unquote_path(supplied)).expanduser().resolve()
        if not datasheet.is_file():
            print(f"Datasheet not found: {datasheet}")
            return 1
        from boardmodeler.cli import _sanitize_subckt

        output = _new_model_path(model_dir(config.default_model_dir), _sanitize_subckt(part))
    except (OSError, ValueError) as exc:
        print(f"The selected path cannot be used: {exc}")
        return 1

    print(f"Saving the model to {output}")
    arguments = [
        "model",
        "build",
        "--part",
        part,
        "--datasheet",
        str(datasheet),
        "--out",
        str(output),
    ]
    if config.internet_access:
        arguments.append("--allow-remote")
    return command(arguments)


def _saved_models(base: Path) -> list[Path]:
    try:
        return sorted(
            (path for path in base.iterdir() if path.is_dir()), key=lambda path: path.name
        )
    except FileNotFoundError:
        return []


def _open_or_test(reader: Reader, command: Command) -> int:
    base = _model_home()
    if base is None:
        return 1
    try:
        saved = _saved_models(base)
    except OSError as exc:
        print(f"Saved models could not be listed: {exc}")
        return 1
    if saved:
        print("Saved model folders:")
        for number, path in enumerate(saved, start=1):
            print(f"  {number}. {path.name}")
    supplied = _read(reader, "Model number or folder path (Enter to go back): ")
    if supplied is None or not supplied.strip():
        return 0
    supplied = _unquote_path(supplied)
    try:
        if supplied.isdecimal() and 1 <= int(supplied) <= len(saved):
            chosen = saved[int(supplied) - 1]
        else:
            candidate = Path(supplied).expanduser()
            chosen = (
                candidate if candidate.is_absolute() or candidate.is_dir() else base / candidate
            )
        chosen = local_path(chosen)
    except (OSError, ValueError) as exc:
        print(f"The selected model folder cannot be used: {exc}")
        return 1
    if not chosen.is_dir():
        print(f"Model folder not found: {chosen}")
        return 1
    action = _read(reader, "Open the saved result or re-test it? [o/T]: ")
    if action is None:
        return 0
    arguments = ["model", "open", "--out", str(chosen)]
    if action.strip().lower() in {"t", "test", "r", "retest", "re-test"}:
        arguments.append("--verify")
    elif action.strip().lower() not in {"", "o", "open"}:
        print("Choose o to open or t to re-test.")
        return 1
    return command(arguments)


def _check_setup() -> int:
    from boardmodeler.cli import _render_doctor_human, doctor_payload

    payload = doctor_payload()
    print(_render_doctor_human(payload))
    completed = load_config().setup_complete
    if not completed:
        print("The setup wizard has not been completed.")
    ready = bool(payload["ok"]) and completed
    print("Setup check: ready" if ready else "Setup check: needs attention")
    return 0 if ready else 1


def run_menu(*, reader: Reader | None = None, command: Command | None = None) -> int:
    """Run the menu; every action uses the same command handlers as the flags."""
    if reader is None:
        reader = input
    if command is None:
        from boardmodeler.cli import main

        command = main
    failed = False
    while True:
        print("\nSpice Maker")
        print("  1. Build a model")
        print("  2. Open or re-test a saved model")
        print("  3. Settings")
        print("  4. Check setup")
        print("  5. Quit")
        choice = _read(reader, "Choose 1-5: ")
        if choice is None or choice.strip() in {"5", "q", "quit"}:
            return 1 if failed else 0
        try:
            if choice.strip() == "1":
                result = _build(reader, command)
            elif choice.strip() == "2":
                result = _open_or_test(reader, command)
            elif choice.strip() == "3":
                result = command(["setup"])
            elif choice.strip() == "4":
                result = _check_setup()
            else:
                print("Choose a number from 1 to 5.")
                continue
        except (OSError, ValueError) as exc:
            print(f"Spice Maker could not complete that action: {exc}")
            result = 1
        failed |= bool(result)
