# Active engineering decisions — Spice Maker Bob

This file records decisions that constrain current work. The complete pre-transition decision log is preserved verbatim in [`docs/evidence/2026-09-28-terminal/history/DECISIONS-before-terminal.md`](evidence/2026-09-28-terminal/history/DECISIONS-before-terminal.md). Older decision numbers cited by code and evidence can be read there.

## D-014 — Datasheet evidence and measured verdicts

A model build freezes the extracted specification before authoring. IBM Bob Shell runs with all tool groups disabled and returns model text; the application writes candidates and judges them with LTspice. PASS requires a real measured artifact and the cited operating conditions. Unmeasured, unsupported, or unreachable rows remain UNKNOWN with their reasons. The quick structural path cannot claim electrical accuracy.

## D-015 — Bob-only provider catalog

`agent_providers.CATALOG` is the only authority for available providers. This edition holds IBM Bob alone. A configured provider unavailable in this edition is refused, never replaced silently. Its key remains in the plain local `data/credentials.bob.json` file inside the extracted copy and must never be logged or passed on a command line. Bob's own license review remains a manual prerequisite.

## D-035 — Identical reviewed core

`shared_core.json` names the provider-neutral modules that remain byte-identical across editions. `tools/shared_core.py --check` and `--compare` enforce this. Provider policy, catalog, config, CLI, and setup remain edition-owned where needed.

## D-052 — Product scope

The product creates SPICE models, symbols, model cards, and tests. Board checking and board findings reports are outside the public product workflow. Legacy board helpers can support internal test circuits but must not be exposed as commands or menu choices.

## D-053 — Terminal front end and optional shortcut (2026-09-28)

The terminal menu supersedes the Qt window, and `Setup.cmd` plus `tools/bootstrap.py` supersede the compiled installer. One local `.venv` runs both the menu and flag commands. `Setup.cmd` explains its actions, discovers a supported CPython 3.14 or asks before a hash-checked per-user Python installation, and uses a hash-bearing wheel requirements export. The setup wizard reproduces the persistent choices previously saved by setup, including the explicit LTspice path and smoke test, model folder, Bob key, Internet switch, and `setup_complete`.

The prior no-shortcut decision is reversed only for an opt-in Desktop shortcut. Bootstrap creates or refreshes it after asking; `Setup.cmd --remove` deletes it. A shortcut failure is nonfatal. The application write guard continues to confine application-owned writes to the extracted folder; bootstrap owns the optional Desktop write. The model engine, Bob's tool-free boundary, and measured-verdict rules remain unchanged.
