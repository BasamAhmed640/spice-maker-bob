# Spice Maker Bob

Spice Maker Bob creates LTspice `.lib` models, `.asy` symbols, cited model
cards, and verification tests. It has no board checker, board/CAD import, or
user-facing board findings report. Existing board-layer helpers are internal
test code only; see [D-052](docs/DECISIONS.md).

Source and checked-in installer version: **1.6.0**. Install it with the
[checked steps](#install-on-windows) below.

## Install on Windows

<!-- install-steps:start -->
Windows shows a blue **Windows protected your PC** box when you start a program that came from
the internet without a paid code-signing certificate. `Install.exe` is not signed, so starting it
from an ordinary browser download shows that box. The steps below avoid the box without switching
off any Windows protection: you download only from this GitHub repository, and you check
`Install.exe` against the checksum published here before you run it. Microsoft Defender still
scans the files.

**Recommended: download with PowerShell.** Open **PowerShell** from the Start menu and run these
lines one at a time. Files downloaded this way carry no "came from the internet" mark, so the box
does not appear.

1. Go to your Downloads folder. If a `spice-maker-bob-main` folder is already there from an earlier
   install, rename it first (for example to `spice-maker-bob-old`) so old and new files do not mix.

   ```powershell
   cd ~\Downloads
   ```

2. Download this repository's ZIP. `--proto =https` refuses anything but HTTPS.

   ```powershell
   curl.exe --fail --location --proto =https -o spice-maker-bob.zip https://github.com/BasamAhmed640/spice-maker-bob/archive/refs/heads/main.zip
   ```

3. Extract it.

   ```powershell
   tar -xf spice-maker-bob.zip
   ```

4. Check the installer. It must print **OK**. If it prints **STOP**, delete `spice-maker-bob.zip` and the
   `spice-maker-bob-main` folder and run nothing from them.

   ```powershell
   if ((Get-FileHash .\spice-maker-bob-main\Install.exe -Algorithm SHA256).Hash -eq '0b44f3b9976b66821f7654bdc066791dd69ee12ce5e4ce90a205b05b4509921b') { 'OK: Install.exe matches the published checksum' } else { 'STOP: Install.exe does not match the published checksum' }
   ```

5. Start the installer.

   ```powershell
   .\spice-maker-bob-main\Install.exe
   ```

**Or download with your browser.** Choose **Code → Download ZIP** on this page. Before you
extract it, right-click the ZIP → **Properties** → tick **Unblock** → **OK**; do this only for the
ZIP you downloaded from this page. Then right-click the ZIP → **Extract All**. Open the folder
that contains `Install.exe`, right-click an empty spot → **Open in Terminal** (Windows 10:
Shift + right-click → **Open PowerShell window here**), and run the check from step 4 with
`.\Install.exe` in place of `.\spice-maker-bob-main\Install.exe`. Double-click `Install.exe` only after
the check prints **OK**. If you forgot to unblock and the box appears, choose
**More info → Run anyway** only after the check has printed **OK**.

Keep it safe:

- Download only from `https://github.com/BasamAhmed640/spice-maker-bob`. Never run a copy that someone
  sends you or that comes from another site.
- The checksum proves the file is the one published in this repository. It cannot prove who
  built it, because `Install.exe` is not signed.
- Do not turn off SmartScreen or Microsoft Defender, and do not change PowerShell's execution
  policy. None of these steps needs that.
- After installing, start the app from the **Spice Maker** shortcut or `Start.cmd` in the same
  folder. The installer creates those files itself, so they carry no mark and do not show the box.

Installer version **1.6.0**, built 2026-09-25. SHA-256 of `Install.exe`:
`0b44f3b9976b66821f7654bdc066791dd69ee12ce5e4ce90a205b05b4509921b`, the same value as in `SHA256SUMS.txt`.
<!-- install-steps:end -->

## Current source engine

New builds in the window, CLI and API default to **Code-built behavioral** with full LTspice
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

The root `AGENTS.md` and `.bob/rules/` files guide work when this repository is opened as an IBM Bob project. Embedded model authoring uses a generated scratch workspace with the same tool restrictions; it does not load repository rules or hooks. The application supplies the model requirements and safety limits in the prompt.

**IBM Bob is the only AI in this edition, in both the UI and the application source.** Bob is a CLI provider, so it declares no HTTP endpoint: an HTTP destination is refused for it rather than guessed. Bob extracts datasheet records when local evidence is unavailable. Model authoring,
AI test planning and repair require the explicit legacy route or a planning opt-in. A model card records measured behavior and every uncovered requirement.

**Full electrical verification is the default.** Its checkbox is beside **GO** in
the build window. It can be unchecked only on **AI authored (legacy)** for a quick structural
draft whose electrical accuracy remains unverified. SETUP has one **INTERNET ACCESS** checkbox for Bob
and the part vendor's supporting-material site. With it off, the app refuses
Bob runs and key checks before launching Bob Shell. [Modes and limitations](docs/QUICK_MODE.md).

**Installer 1.6.0 history: template-first buck models.** For a supported buck-converter pinout,
the app fills a known-convergent template from cited datasheet rows, labels any
template defaults and judges the candidate with LTspice. Bob receives measured
failures for bounded repair. If repair cannot improve the model, the simulator-
measured template may still be delivered with its FAIL and UNKNOWN rows visible.
Other device classes continue through Bob authoring. Template parameters are
provenance, not verification; PASS still requires an observed simulator artifact.

**This build is portable.** Install it with the [checked steps](#install-on-windows).
The animated installer puts the app in `app/` in that same
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

## After installing

Install with the [checked steps](#install-on-windows) at the top of this page. The checked-in
1.6.0 installer retains the animated pepper setup. INSTALL.txt contains instructions;
SHA256SUMS.txt records the installer's checksum, which catches a damaged or altered download
but cannot prove who built it.
Python is bundled. LTspice and IBM Bob Shell are separate prerequisites. This build is unsigned.

The installer carries CPython 3.14, hash-checked wheels and its generated
`env/requirements.txt`. It creates this extracted folder's `.venv` from those pins
without downloading packages during installation or using the computer's Python.
`Boardmodeler.cmd` uses that environment for command-line work; `Start.cmd` opens
the separately frozen GUI, because the `.venv` does not include Qt.

A fresh ZIP downloaded from GitHub main was installed and checked on Windows:
the frozen GUI opened, the installer created its local Python 3.14.2 `.venv`,
the selected LTspice path worked, and a saved model passed 8/8 simulator retests.
The fixture-based build still had 21 UNKNOWN requirements, so these checks do
not establish full device accuracy. [Release evidence](docs/evidence/2026-09-25-release/REPORT.md).

Open IBM Bob Shell once to review and accept IBM's license. Then open SETUP, use
**BROWSE** to choose the LTspice executable, run its smoke test and save a Bob API key.
Bob Shell uses an Inference-scoped key through the process environment; the key is kept in
`data/credentials.bob.json` inside this folder — plain text, **not encrypted**, so anyone who
can read the folder can read the key — and is never passed on the command line. See the
[Bob Shell setup documentation](https://bob.ibm.com/docs/shell/getting-started/install-and-setup).
No interactive account login is required by this app.

**What is and is not a sandbox.** The project `.venv` isolates Python packages only, and
`.bobignore` only hides files from Bob's context. Neither is an OS sandbox: neither restricts
filesystem, network or process access by this app, LTspice or the model provider. What does
contain this app: LTspice runs only from the path you choose in SETUP (nothing searches for
it); the simulator gets an allowlisted environment, so no API key reaches it; Bob Shell runs
with every tool group disabled (no Bash, no file tools) and only returns model text; and only
the application writes files (decks and the candidate model). The key file,
`data/credentials.bob.json`, is plain text inside this folder. The optional `sim` extra (`spicelib`, not in `requirements.txt`) checks LTspice's default
install locations when it is imported; the app imports it only after an LTspice path is set.

If existing settings are incompatible, click **USE IBM BOB** and **SAVE** in SETUP.
The app refuses incompatible settings until that explicit choice; it never silently
substitutes an agent or displays the incompatible agent's name.

## Make and test a model

Enter the exact part number, select its PDF and a save folder, then press **GO**.
The default engine builds supported behavioral implementations in code. Bob may be needed for
extraction, while exact reviewed or cached evidence can allow a build with no Bob calls. AI model
authoring and repair run only after selecting **AI authored (legacy)**. When an AI stage runs,
the app sends its relevant text through Bob Shell with all tool groups disabled. SETUP holds the persistent
model folder and the one **INTERNET ACCESS** switch; **FULL VERIFICATION** is beside
GO for each build. CANCEL requests cancellation;
results provide Open model folder, Run tests again and Install into LTspice actions.
Bob controls model selection and reasoning. This application does not invent a maximum
thinking flag that Bob Shell has not documented.

For command-line reverification, `model test --out <saved model folder>` reloads
the saved model and reruns its checks. Version 1.6.0 has no `model open` CLI
command; the result window has **Open model folder** and **Run tests again**.

The extraction prompt includes the exact requested part. The same family PDF's cached
rows cannot be silently reused for a different part suffix. A repeated PDF can grant or
revoke remote permission without changing its content identity. Invalid extraction JSON
gets bounded repair; failures identify the parse location with secrets redacted before
any diagnostic excerpt is shortened.

The frozen specification owns limits, citations and conditions. On the default route, local code
writes the candidate. On the explicit legacy route, Bob proposes model text; only the application
writes it. Legacy repairs receive the current model and observed results, retain the best candidate
and stop at the iteration/stall limit. Revalidation checks the current bytes against simulator evidence.

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

Rebuild with `installer/build.ps1 -Version 1.6.0`. The build checks the frozen GUI before
packaging and refreshes Install.exe, INSTALL.txt and SHA256SUMS.txt at the repository
root. These generated files must be committed for Code → Download ZIP to update.
After a rebuild, put the new version and checksum into [Install on Windows](#install-on-windows);
`tests/test_install_instructions.py` fails until they match.
See [installer details](installer/README.md) and [current status](docs/STATUS.md).
