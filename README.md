# Spice Maker

> **This build accepts the IBM Bob API only.** The provider row is absent from the SETUP
> page (which says so), the key row reads `BOB API KEY`, the window title ends in
> `· IBM Bob only`, and a config naming any other provider falls back to Bob. The general
> build — every vendor in `agent_providers.CATALOG` — is
> [`spice-maker`](https://github.com/BasamAhmed640/spice-maker).


**Give it a datasheet and a part number; agents author an LTspice model; real simulator
runs judge it against the datasheet's own rows; you get a `.lib`, a symbol and a card
saying exactly what was tested.** That is the product. Everything below the fold is
supporting machinery, and the board/circuit/UI layers date from an earlier, wider spec.

```powershell
uv sync --all-extras
uv run boardmodeler doctor            # confirms LTspice is usable (real smoke test)
uv run boardmodeler model build `
    --part TPS54320 --subckt TPS54320 `
    --requirements fixtures/regulator/tps54320/requirements.json `
    --bindings     fixtures/regulator/tps54320/probes.json `
    --out build/tps54320
uv run boardmodeler model test --out build/tps54320     # re-judge any time
uv run boardmodeler model install --out build/tps54320 --user-lib --apply
```

**Before the first run:** open **SETUP** in the window and paste an agent API key — that is
the whole authentication story; there is no login anywhere in this application. **IBM Bob** is
the default provider and needs Bob Shell installed
(`powershell -c "irm -Uri https://bob.ibm.com/download/bobshell.ps1 | iex"`, Node ≥ 24) plus a key from
bob.ibm.com → API keys with **Scope = Inference** (an *Inference* key needs no team id; a
*general* key does). The same row also takes a plain vendor key from OpenAI, Anthropic, Google,
DeepSeek, OpenRouter, xAI, Groq, Mistral or OpenCode Zen / Go, and those run over HTTP with no
CLI and no extra install. Every key goes to the Windows credential store — never to a config file, a project
directory, a manifest or a log line. Setup is one page and holds only what persists: the LTspice
path (with a smoke test), the agent provider and key, the model id when the provider takes one,
the folder finished models go to, the read-only LTspice user library path, and the
web-reinforcement switch. Installing a finished model is always the window's **Install into
LTspice** action, which copies into the per-user library. The main window holds nothing but the
part number, the datasheet, the save location, **GO** and the progress detail. Without an agent
the run stops immediately with `BLOCKED` naming what is missing — it never substitutes another
provider. A build costs a few agent turns plus about ten seconds of simulation: the TPS54320
fixtures judge 38 datasheet rows in **10.7 s** of real LTspice work once the model exists, and a
repeated run makes **zero** further extraction calls.

## Install

`releases\SpiceMaker-win-Setup.exe` is a one-click Velopack installer: it unpacks the frozen
application under `%LocalAppData%\SpiceMaker`, adds a Start Menu entry, and starts it — with
the pepper splash while it unpacks and no wizard pages to click through. It installs **only
this application**: no LTspice, no Python, no agent CLI is downloaded or installed by it.
LTspice is discovered by the app (or pointed at in SETUP) and smoke-tested there, and
`doctor` says plainly when it is missing. Build the installer yourself with
`installer\build.ps1 -Version <x.y.z>`; see `installer/README.md`.

## Two builds of one product

The agent provider list is data (`src/boardmodeler/agent_providers.py`), so a restricted
build is one tuple, not a fork of the code (D-015):

|Build|Accepts|Use it for|
|---|---|---|
|`spice-maker` (this repository)|Bob **and** every vendor key in the catalog|Trying providers, comparing models, day-to-day work|
|`spice-maker-bob`|The IBM Bob API only|The Bob-native workflow: no provider row on SETUP, `BOB API KEY` only|

What each piece guarantees:

* **The spec is frozen before the agent starts.** Limits, tolerances, probe bindings and
  citations live in `spec/characteristics.json`; the agent may write only
  `model/<SUBCKT>.lib` and `.asy`. A changed spec aborts the build as `UNKNOWN(spec_tampered)`.
* **The harness owns the verdicts.** One probe deck per bound characteristic runs in real
  LTspice; the measured value is compared to the cited limit. A probe that cannot answer
  its question (no crossing, signal not saved, run truncated, port missing) is `UNKNOWN`
  with the reason — never `PASS`.
* **Unreachable rows stay visible.** Datasheet rows no probe can exercise (internal
  oscillator behavior, thermal response, package facts) are listed on `MODEL_CARD.md` with
  a `not_testable_reason`, so a reader sees the size of the claim, not a summary of it.

The older paths — circuit checking, the board demonstration, the desktop UI — remain in
the tree and are described further down; they are not on the model-authoring path.

## Honesty invariants

These are enforced in code, not by convention, and they are the reason to trust a
report:

* No `PASS` without an observed simulator artifact. A missing measurement, an
  uncovered assertion window, or a signal that never appears becomes `UNKNOWN`.
* A requirement whose behaviour the model's capability probe did not establish as
  `supported` is gated to `UNKNOWN` — an average vendor model cannot silently
  "pass" start-up.
* Every cited requirement is verified against the text of the page it cites; an
  invented citation fails verification and its dependent tests evaluate to
  `UNKNOWN`.
* A "must not occur" requirement additionally requires the run to have reached the
  end of its window with the signal observable: the absence of a crossing is not a
  pass on its own.
* Repair is capped and constrained: it may only touch `models/candidates/<n>/`, and
  relaxing a tolerance, deleting a test, editing evidence, or editing the circuit
  raises `RepairViolation` and stops the loop.
* Nothing is ever written into the LTspice installation, and vendor model bytes are
  never copied into an export.

## Setup

```powershell
uv sync --all-extras              # Python 3.14 venv with every dependency
uv run boardmodeler doctor --json # LTspice discovery + smoke test, reader backend, OCR, credentials
uv run boardmodeler setup         # optional: record the LTspice path, provider, and data policy
uv run boardmodeler ui            # optional: the desktop application
```

`doctor` reports the real smoke-test measurement (an RC step's analytic 0.632 V,
±2 %), not just "found": if the simulator is absent or its outputs unusable, that is
what it says.

## Workflows

### The integrated board demonstration

A complete board — 12 V input, buck to 3V3, LDO to 1V8, reset circuit, straps,
sideband, and an unmodelled PCIe switch kept explicitly outside dynamic coverage —
is built from committed fixtures and checked end to end:

```powershell
uv run boardmodeler demo build --out build/demo
uv run boardmodeler circuit check --project build/demo --json --out build/demo-results.json
uv run boardmodeler run mutations --project build/demo --report build/mutation-report.json
```

`demo build` reports its requirement, test-case, and static-finding counts;
`circuit check` prints one line per test with its status; `run mutations` injects
each fault into its own copy, runs the check, and records whether the fault was
detected — hashing the original project before and after to prove the mutation did
not leak into it.

### Testing a circuit

```powershell
uv run boardmodeler run tests --project <project-dir> --scope circuit_compliance --json
uv run boardmodeler circuit check --project <project-dir> --circuit <schematic.asc> --fault-matrix
```

### Extracting from documents

```powershell
uv run boardmodeler extract --project <project-dir> --doc <datasheet.pdf> [--allow-remote]
```

`--allow-remote` is required for any provider that leaves the machine, and it is
checked against each document's own `remote_inference_allowed` flag and
classification; the disclosure (provider, endpoint, pages, characters) is printed
and recorded in the run manifest. The default provider is the offline fixture
replay, so repeat runs make zero requests.

### Exporting a model

```powershell
uv run boardmodeler export --project build/demo --out build/demo-export
```

The export carries only relative paths, hashes every file into `manifest.json`,
records the vendor model's hash while refusing to copy vendor bytes, and lists every
requirement with no dynamic test in `coverage.json`.

## Limits we state rather than hide

* **OCR is unavailable on this machine** (`tesseract` absent). Pages that need OCR
  produce an explicit evidence gap; OCR text is never presented as embedded text.
* **The ported TI vendor model is evidence, not a workhorse.** It simulates, but a
  2.1 ms application run hit a 600 s cap, so the dynamic tests and the demo use the
  generated behavioural template while the vendor model's capability record states
  exactly which behaviours its probes established (see `docs/DECISIONS.md` D-010).
* **Real PCIe-switch qualification is `BLOCKED`.** No public documentation exists for
  the device class the synthetic fixture stands in for, so the fixture is labelled
  `origin=TEST_FIXTURE` everywhere and cannot be presented as device data.
* **No temperature or statistical claims.** Without modelled temperature dependence
  there is no temperature validation, and no distributions are inferred from
  min/max limits.

## Layout

|Path|Contents|
|---|---|
|`src/boardmodeler/domain/`|Record schemas (pydantic), enums, hashing, ids, constrained expression AST|
|`src/boardmodeler/simulation/`|LTspice batch invocation, log parsing, `.raw` readers, backend selection|
|`src/boardmodeler/verification/`|Assertion evaluation, vacuous-pass guards, corners, scenarios, engine|
|`src/boardmodeler/schematic/`|`.asc` parse/generate, SPICE netlist parsing, neutral CSV model, static checks, mutations|
|`src/boardmodeler/models/`|Vendor-original store, behavioural templates, capability probes, symbol generation|
|`src/boardmodeler/requirements/`|Requirement extraction, validation, source-consistency review|
|`src/boardmodeler/documents/`|PDF text/page extraction, document store, OCR interface, chunking|
|`src/boardmodeler/providers/`|Fixture / HTTP inference / Bob providers behind one protocol|
|`src/boardmodeler/pipeline/`|Stage chain, baseline freeze, bounded repair, child-process worker|
|`src/boardmodeler/reporting/`|Export, model card, HTML report|
|`src/boardmodeler/security/`|Credentials, path guards, subprocess guard, data policy|
|`src/boardmodeler/ui/`|PySide6 desktop application (thin client over the same pipeline)|
|`fixtures/`|Committed test fixtures (synthetic switch contract, demo board)|
|`docs/`|`PLAN.md`, `STATUS.md`, `DECISIONS.md`, `INTERFACES.md`|

## Rules

See `AGENTS.md` — the short version: run `uv run pytest -q` before claiming
anything works, never write into the LTspice installation, never record a result
that was not observed, and label every synthetic fixture as synthetic.
