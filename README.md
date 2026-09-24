# Spice Maker Bob

**Safety update:** SETUP requires you to choose an LTspice executable with **BROWSE** and save it. The app does not search installed programs or adopt an inherited `LTSPICE_EXE`. Bob Shell receives the complete prompt through stdin with its read, edit, execute, MCP, skill, todo, subagent and mode tools disabled. The application writes Bob's model text and runs LTspice itself; a Bob reply cannot mark a model verified.

The root `AGENTS.md` and `.bob/rules/` files guide work when this repository is opened as an IBM Bob project. Embedded model authoring uses a generated scratch workspace with the same tool restrictions; it does not load repository rules or hooks. The application supplies the model requirements and safety limits in the prompt.

**IBM Bob is the only AI in this edition, in both the UI and the application source.** Bob is a CLI provider, so it declares no HTTP endpoint: an HTTP destination is refused for it rather than guessed. Bob reads a datasheet, authors an LTspice model and repairs it using actual simulator
feedback. A model card records measured behavior and every uncovered requirement.

**Full electrical verification is the default.** Quick mode remains available in SETUP and reports electrical accuracy as unverified. [Modes and limitations](docs/QUICK_MODE.md).

**This build is portable.** Extract the GitHub **Code > Download ZIP** archive and run
**Install.exe** inside it. The animated installer puts the app in `app/` in that same
folder. Open `Start.cmd` next time. First launch asks you to choose LTspice and a model
folder inside this extracted folder, and enter your key. It never restores settings
or keys from an older installation. [Storage and fresh-start instructions](docs/PORTABLE_STORAGE.md).


**SAVE & CHECK KEY** now verifies a newly saved key in the background, with a 15-second wait and clear verified/rejected/unverified results. [Credential safety and check details](docs/API_KEY_CHECK.md).

Datasheet extraction and model verification now recover smaller requests, preserve failed-run feedback, and report untested numeric requirements honestly. See [coverage and reliability](docs/DATASHEET_ROBUSTNESS.md).

IC symbols now use a consistent local layout with verified model pin order. See
[standard symbols](docs/STANDARD_SYMBOLS.md).

LM358 now has reviewed datasheet extraction and real dual-amplifier checks. See
[measured coverage and limitations](docs/LM358_VALIDATION.md).

GO now shows a continuously updating **ELAPSED HH:MM:SS** clock, preserving the
final duration. See [what the agents and simulator do](docs/AGENT_WORKFLOW.md).

## Install on Windows

On the **main** branch, choose **Code → Download ZIP**, extract the archive and run
**Install.exe** beside this README. The included installer retains the animated
pepper setup. INSTALL.txt contains instructions; SHA256SUMS.txt authenticates the installer.
Python is bundled. LTspice and IBM Bob Shell are separate prerequisites. This build is unsigned.

Open IBM Bob Shell once to review and accept IBM's license. Then open SETUP, use
**BROWSE** to choose the LTspice executable, run its smoke test and save a Bob API key.
Bob Shell uses an Inference-scoped key through the process environment; the key is kept in
`data/credentials.bob.json` inside this folder — plain text, **not encrypted**, so anyone who
can read the folder can read the key — and is never passed on the command line. See the
[Bob Shell setup documentation](https://bob.ibm.com/docs/shell/getting-started/install-and-setup).
No interactive account login is required by this app.

If existing settings are incompatible, click **USE IBM BOB** and **SAVE** in SETUP.
The app refuses incompatible settings until that explicit choice; it never silently
substitutes an agent or displays the incompatible agent's name.

## Make and test a model

Enter the exact part number, select its PDF and a save folder, then press **GO**.
The selected datasheet and model text are sent to Bob. SETUP also holds the persistent
model-folder and supporting-web-search preference. CANCEL requests cancellation;
results provide Open model folder, Run tests again and Install into LTspice actions.
Bob controls model selection and reasoning. This application does not invent a maximum
thinking flag that Bob Shell has not documented.

The extraction prompt includes the exact requested part. The same family PDF's cached
rows cannot be silently reused for a different part suffix. A repeated PDF can grant or
revoke remote permission without changing its content identity. Invalid extraction JSON
gets bounded repair; failures identify the parse location with secrets redacted before
any diagnostic excerpt is shortened.

The frozen specification owns limits, citations and conditions. Bob proposes model
text; only the application writes candidates. Repairs receive the current model and observed results,
retain the best candidate and stop at the iteration/stall limit. Repeated valid builds
can skip authoring calls; a fresh process re-establishes simulator evidence.

## What is actually validated

There are 11 regulator/supply probes and 9 electrical I/O probes. Each PASS needs an
observed LTspice artifact and the cited operating conditions. Different supply, load,
temperature or timing conditions remain separate. Missing signals, unverified citations,
unsupported behavior and incomplete runs cannot become PASS.

The TPS54320 fixture has 38 rows: 9 bind to 8 regulator probes and 29 remain explicitly
untested. Fixture success establishes the harness, not every real device. Acceptance by
the broad analogue classifier does not establish op-amp gain, offset, bandwidth or slew
coverage. No complete LM358 qualification is claimed. Full temperature/statistical
behavior needs its own modeled dependence and evidence.

Vendor IBIS/AMI/Touchstone sources can be imported with provenance. These reduced probes
do not qualify high-speed channel, eye, BER or protocol behavior; compatible external
validation is required. See [coverage and limitations](docs/FAST_ACCURATE_MODELS.md).

## Development

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m boardmodeler.cli doctor --json
.\.venv\Scripts\python.exe -m boardmodeler.cli ui
```

The `.venv` is inside this project and `requirements.txt` pins the runtime
dependencies. This source setup requires Python 3.14 already installed; the rebuilt
`Install.exe` bundles Python for users who do not have it. For development tests,
install `requirements-dev.txt` into the same `.venv` and run pytest there.

`--backend api` is a compatibility alias for the Bob API-key adapter in this edition.
The fixture/scripted backends remain clearly labeled deterministic test tools. No other
AI catalog or author transport ships in this repository.

Rebuild with `installer/build.ps1 -Version 1.4.0`. The build checks the frozen GUI before
packaging and refreshes Install.exe, INSTALL.txt and SHA256SUMS.txt at the repository
root. These generated files must be committed for Code → Download ZIP to update.
See [installer details](installer/README.md) and [current status](docs/STATUS.md).
