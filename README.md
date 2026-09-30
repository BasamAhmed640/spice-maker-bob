# Spice Maker Bob

Spice Maker Bob turns a part number and datasheet PDF into an LTspice `.lib` model, `.asy` symbol, model card, and verification results. IBM Bob Shell proposes the model; LTspice runs the checks. A PASS applies only to a measured datasheet row at its recorded conditions. Unmeasured behavior stays UNKNOWN.

## Get started on Windows

1. On GitHub, choose **Code → Download ZIP** and extract the whole ZIP to a folder you can edit.
2. Double-click **Setup.cmd** in the extracted folder. Read its opening summary and answer its questions.
3. Double-click **Start.cmd** for the text menu. **Boardmodeler.cmd** runs flag commands.

Setup needs Windows 10 or 11 on x64 or ARM64. It looks for a real CPython 3.14 without running a `python` command from PATH. If none is found, it asks before downloading the pinned Python 3.14.7 installer from python.org, checking its SHA-256, and installing it for the current user without administrator rights or a PATH change. Declining stops setup. The installer and exact hashes are listed in [`tools/python-install-pins.txt`](tools/python-install-pins.txt).

**IBM Bob is the only AI in this edition, in both the menu and the application source.** Bob is a CLI provider, so it declares no HTTP endpoint: an HTTP destination is refused for it rather than guessed. Bob extracts datasheet records when local evidence is unavailable. Model authoring,
AI test planning and repair require the explicit legacy route or a planning opt-in. A model card records measured behavior and every uncovered requirement.

**Full electrical verification is the default.** A quick structural draft is available only on the explicit legacy engine (`model build --engine legacy_ai --sanity`); its electrical accuracy remains unverified. Setup has one **Internet** setting for Bob and the part vendor's supporting-material site. With it off, the app refuses Bob runs and key checks before launching Bob Shell. [Modes and limitations](docs/QUICK_MODE.md).

Setup creates `.venv` in this folder and downloads eight pinned Python packages from PyPI. It checks every package hash before installation, installs wheels only, and honors `HTTPS_PROXY`. It does not download LTspice, IBM Bob Shell, a datasheet, or a vendor model. Install [IBM Bob Shell](https://bob.ibm.com/docs/shell/getting-started/install-and-setup) separately and open it once to review and accept IBM's license. Setup asks you to paste the path to an existing LTspice executable, runs a real smoke test, asks for a model folder inside this copy, and saves the Internet setting and your Bob API key. Bob Shell is the only provider in this edition, so the key prompt says **BOB API KEY**. The key prompt is hidden. The key is a plain local file at `data/credentials.bob.json`: anyone who can read this folder can read it. Keep the folder private, and do not share `data/`.

Setup offers an optional **Spice Maker** Desktop shortcut that runs `Start.cmd`. It asks before creating it. Re-running setup refreshes it; `Setup.cmd --remove` removes it. The shortcut is optional, so a locked-down Desktop does not prevent the app from working.

Windows may show a security prompt when you launch a script extracted from a downloaded ZIP. Check the source and contents of `Setup.cmd` before choosing to run it. If setup stops, its window stays open and prints what to fix. Use a short, writable extraction path; all app-owned data remains inside it.

To uninstall, run `Setup.cmd --remove` if you created the shortcut, then delete the extracted folder. If Setup installed Python for you, remove **Python 3.14** separately in **Windows Settings → Installed apps**. Deleting this copy does not uninstall LTspice or Bob Shell.

| Platform | Support | Observed verification |
| --- | --- | --- |
| Windows 11 x64 | Targeted | Full source ZIP setup, eight pinned packages, launchers, shortcut, and LTspice smoke check on the developer machine. The pinned x64 Python installer also passed on a fresh Windows GitHub Actions runner. |
| Windows 11 ARM64 | Targeted | The pinned ARM64 Python installer passed on a fresh GitHub Actions runner; full source ZIP setup was not tested there. |

Windows 10 x64/ARM64 are setup targets but were not tested in this change. See [`docs/STATUS.md`](docs/STATUS.md) for exact results and limits.

## Make and revisit a model

`Start.cmd` opens a menu to build a model, open or re-test a saved model, change settings, or check setup. A datasheet can be dragged into the prompt; quoted paths are accepted. You can also use the flag interface:

```text
Boardmodeler.cmd doctor --json
Boardmodeler.cmd setup --json
Boardmodeler.cmd model build --part TPS54332DDA --datasheet C:\path\to\sheet.pdf --out models\TPS54332DDA
Boardmodeler.cmd model open --out models\TPS54332DDA --json
Boardmodeler.cmd model test --out models\TPS54332DDA --json
```

Full electrical verification is the default. `model build --engine legacy_ai --sanity` requests a quick structural draft (the code-built engines need full verification); its electrical accuracy remains unverified. A missing LTspice path, Bob Shell, Bob key, or network permission blocks a build with its reason. No provider is substituted silently. Setup never searches this PC for LTspice; you provide its path.

Bob Shell receives its prompt through stdin with tool groups disabled. The application writes Bob's candidate and runs LTspice itself; Bob's reply cannot mark a model verified. With Internet access off, the app refuses Bob runs and key checks before launching Bob Shell. It has no telemetry. LTspice receives an allowlisted child environment without the Bob API key. The app writes its own files inside the extracted folder, never into the LTspice installation or library.

For the exact storage paths and key tradeoff, see [`docs/PORTABLE_STORAGE.md`](docs/PORTABLE_STORAGE.md). For model coverage and limits, see [`docs/FAST_ACCURATE_MODELS.md`](docs/FAST_ACCURATE_MODELS.md).

## Current source engine

New builds in the text menu, CLI and API default to **Code-built behavioral** with full LTspice
verification. AI extraction is the only default AI stage and can be skipped for matching cached,
supplied or exact reviewed evidence. Local code binds independent tests, builds a typed design from
cited inputs and renders SPICE. It does not invoke an AI author, planner or repair loop by default.

The implemented behavioral families are the peak-current buck and eight-pin dual op amp.
Each part still needs supported pins, essential cited inputs and independent tests. Missing evidence
or an unresolved package can produce **BLOCKED** with diagnostics and no model. Microcontrollers,
FPGAs, CPLDs, processors and SoCs are refused on every route; a family hint cannot bypass the gate.

- `behavioral` is the default code-built route. Unsupported parts stop; no other engine runs as a fallback.
- `legacy_ai` explicitly selects AI authoring, full-mode AI planning and bounded repair.
  `--plan-tests` separately opts a local route into extra AI planning.
- `pin_only` explicitly creates a limited pin interface with no device function or electrical
  accuracy claim. It supports one supply rail and one ground; a second required rail is refused.

Quick structural drafts require `legacy_ai`; the two code-built engines require full verification.
Old saved requests keep their recorded route, and requests without an engine field retain their
historical legacy interpretation. See [the workflow](docs/AGENT_WORKFLOW.md),
[quick-mode limits](docs/QUICK_MODE.md) and [current evidence](docs/STATUS.md).

The current evidence does not establish full-family qualification: TPS54332 has four nominal
qualification passes and twelve mandatory UNKNOWN gaps, while TPS54331 remains BLOCKED on UVTH,
package and independent coverage. These source changes do not constitute a rebuilt installer release.
The planned M6 publication gate still needs source-confirmed package selection, symbol pin numbers
and discrete pin order. Current file hashes, pin-order records and the TPS54331 pad guard do not
complete that gate.
The [engine acceptance report](docs/evidence/2026-09-29-engine-refactor/REPORT.md) records exact
delivered hashes, measured timings, row counts and the remaining limits.

Replayed extraction records must pass citation checks against the available source document again.
An old `citation_verified=true` flag cannot certify itself; missing or failed checks are saved as
unverified in the new run and cannot supply a qualified test reference.

**Safety update:** SETUP requires you to choose an LTspice executable with **BROWSE** and save it. The app does not search installed programs or adopt an inherited `LTSPICE_EXE`. Bob Shell receives the complete prompt through stdin with its read, edit, execute, MCP, skill, todo, subagent and mode tools disabled. The application writes Bob's model text and runs LTspice itself; a Bob reply cannot mark a model verified.

## Setup options

`Setup.cmd --yes` uses saved answers, and `--no-install-python` refuses a Python download. Pass `--ltspice`, `--model-dir`, `--provider bob`, `--internet on|off`, `--shortcut yes|no`, and `--shortcut-dir` for scripted setup. `--key-env NAME` names an environment variable containing the Bob API key; the key value itself never belongs on a command line. `Setup.cmd --remove` removes the shortcut made by this copy.

## Development

Source development requires Python 3.14. The user setup requires only the tools that ship with Windows.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

`uv.lock` is the developer lock. `requirements.txt` is its hash-bearing runtime export and has no editable project install. The launchers put `src` on `PYTHONPATH` instead. [`AGENTS.md`](AGENTS.md) contains the repository rules. `pyproject.toml` currently declares **Proprietary**; the owner has not chosen publication license terms yet.
