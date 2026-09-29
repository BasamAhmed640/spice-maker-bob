# Terminal transition verification — Bob edition

Date: 2026-09-28. Branch: `terminal-app`. Base: `main` at `cf6b430`.

## Scope and integrity

The Qt window, frozen installer, their tests, and installer-only tools were removed. `Setup.cmd`, `Start.cmd`, `Boardmodeler.cmd`, a standard-library bootstrap, and a text wizard/menu were added. IBM Bob Shell remains the sole catalog provider and the saved key remains in `data/credentials.bob.json`. No protected model-engine file was edited: all 63 checked Bob engine files, including `pipeline/make_model.py`, match the base byte-for-byte. `uv run python tools/shared_core.py --check` reported `shared core intact: 44 files`, and `--compare ..\spice-maker-terminal` reported `identical: 44 files`.

This work used a fresh clone; the original source folder with uncommitted buck work was not modified. No live Bob authoring call or real Python installer ran on the developer PC.

## Pin evidence

`Get-FileHash requirements.txt -Algorithm SHA256` returned `4AAC02951F71F13E1A637EE51F54AB41F5D3D67C3EB53B8D68F0454616A28DA2`.

`Get-FileHash tools\python-install-pins.txt -Algorithm SHA256` returned `DA0DA1A9670AE49E219C83647D8D84D6EA8CFD9457B5A2D1BE0F627F3775E8E2`.

Both 3.14.7 Python installers were downloaded but not run locally. Their size, SHA-256, and Authenticode signature were checked against the pins, which match `winget show Python.Python.3.14`: x64 `9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649`; ARM64 `9a3fe120cc81bc2cb099550f794d8356811f96a86c7f438519243c3485db928d`. After push, [GitHub Actions run 36501631540](https://github.com/BasamAhmed640/spice-maker-bob/actions/runs/36501631540) passed both jobs: the exact pinned installers ran per-user on fresh x64 and ARM64 Windows runners and registered CPython 3.14 with the expected platform.

## Test commands and observed output

Baseline fresh clone: `.venv\Scripts\python.exe -m pytest -q` returned `1368 passed, 55 failed, 156 skipped, 8 errors` without local LTspice configuration. With `LTSPICE_EXE` set to the existing LTspice executable it returned `1369 passed, 55 failed, 155 skipped, 8 errors`. The eight errors come from missing ignored `models/T1-tps54332/spec/requirements.json`. Baseline Ruff lint passed.

Current checks:

```text
uv run pytest -q tests/setup tests/test_terminal_menu.py tests/test_cli_model.py tests/test_portable_storage.py
51 passed in 13.52s
uv run ruff check .
All checks passed!
uv run ruff format --check .
250 files already formatted
```

Ruff format excludes two protected, already-unformatted baseline engine files (`authoring/sanity.py` and `simulation/ltspice.py`) rather than editing them. The broader baseline suite was not made green by the terminal change; its missing ignored fixture and assumptions about a configured provider or LTspice remain.

`git archive --format=zip HEAD` was extracted without `.git` into `Bob café Ω clean source`. On Windows 11 x64 with registered CPython 3.14.2, the following command exited 0 without installing Python:

```text
Setup.cmd --no-install-python --yes --ltspice <existing LTspice.exe> --model-dir models --provider bob --internet off --key-env SPICE_MAKER_TEST_KEY --shortcut yes --shortcut-dir <temporary shortcut folder>
Successfully installed annotated-types-0.8.0 numpy-2.5.3 pydantic-2.13.5 pydantic-core-2.46.5 pypdf-6.19.0 pypdfium2-5.13.0 typing-extensions-4.16.0 typing-inspection-0.4.4
LTspice smoke test passed.
Settings saved in this extracted copy.
Shortcut ready: <temporary shortcut folder>\Spice Maker.lnk
```

The `.venv` held exactly the eight runtime packages plus pip, with no Qt. `Boardmodeler.cmd doctor --json` returned `ok: true`. The shortcut binary contained exact UTF-16 paths to `Start.cmd`, the working directory, and `pepper.ico`, including `Ω`; each target exists. A second setup run exited 0 and refreshed the shortcut; `Setup.cmd --remove --shortcut-dir` exited 0 and removed the `.lnk` and its marker. Piped menu and `model open` behavior passed the focused tests; the Bob `model open --json` payload now has the same field set as the general edition.

The Windows launcher tests use stub pins, registry roots, and installer executables; they do not run a real Python installer. They passed for installed Python, install/decline, wrong hash, re-detection, Store/MSYS2 decoys, and unsupported architecture. Bob-specific isolated archive checks also passed: a tampered requirements hash exited 1 before package installation (`pip list` showed only pip); a dead proxy exited 1 after five retries, again with only pip installed; a 209-character path exited 1 before Python discovery; and a wrong LTspice path exited 1 with no config file saved. All gave plain setup explanations, with no traceback or live call.

Bob build/test JSON key sets and exits for safe blocked inputs match its base. `doctor --json` and `setup --json` key sets match its base. The requested setup flags were added, `ui` and `--installer` removed, and Bob's missing `model open` flag was added for the menu. Source `.cmd` files in the ZIP have CRLF endings.

## Remaining verification

- The real Python install step passed on fresh GitHub Actions x64 and ARM64 runners. The complete Setup.cmd path from no Python to app-ready has not been tested on a clean PC; local setup used already-registered Python.
- Windows 10, ARM64, and a clean machine without Python/LTspice/developer tools have not been tested end to end locally.
- A live Bob Shell key check and a completed LM358 build were not run; both would make external requests. There is no bundled or cached LM358 author.
- `pyproject.toml` still declares `Proprietary`; publication license terms are a question for the owner. No license was chosen here.

## Change size

The initial Bob transition commit reported `120 files changed, 4075 insertions(+), 12967 deletions(-)` from `git show --shortstat`. At the verified final source commit, `git diff --shortstat cf6b430..HEAD` reported `121 files changed, 4394 insertions(+), 12967 deletions(-)`.
