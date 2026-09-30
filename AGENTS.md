# AGENTS.md — repository rules

## Commands

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m boardmodeler.cli doctor --json

# the product: datasheet -> agent-authored, simulator-judged model
.\.venv\Scripts\python.exe -m boardmodeler.cli
.\.venv\Scripts\python.exe -m boardmodeler.cli model build --part TPS54320 --datasheet <pdf> --out build/tps54320
.\.venv\Scripts\python.exe -m boardmodeler.cli model test --out build/tps54320
```

## The model-maker path (D-014)

The text menu defaults to full electrical verification. Quick structural checks remain an explicit option and carry UNKNOWN electrical status. Never describe a structural check as measured accuracy. Bob Shell runs with all tool groups disabled; it returns model text, and the application alone writes the candidate and runs LTspice.

* `authoring/` is the engine: `spec.py` (the frozen datasheet rows), `probes.py` (one deck
  per characteristic), `harness.py` (run + judge), `backends.py` + `loop.py` (the agent),
  `card.py` (deliverables). `pipeline/make_model.py` chains them; the text menu and
  `boardmodeler model …` call that same chain.
* The **spec is frozen**: `spec/characteristics.json` is hashed before the agent starts and
  re-checked after every turn. A changed spec stops the build as `UNKNOWN(spec_tampered)`;
  the application may write only `model/<SUBCKT>.lib` and `model/<SUBCKT>.asy`.
* A row that a probe cannot answer is `UNKNOWN` with its reason; a datasheet row no probe can
  reach keeps a written `not_testable_reason` and appears on the card. Never stretch a probe
  to cover a row it does not exercise, and never relax a limit to make a model pass.
* The text menu asks for a part number and datasheet per build and calls
  `make_model(request, progress=...)`. The setup wizard owns persistent choices:
  LTspice path, Bob key, model folder and Internet access. The menu also exposes
  saved-model open/retest and doctor. Do not add per-build questions to setup.
* The agent is reached with an **API key, never a login** (D-015). Which providers a build
  accepts is `agent_providers.CATALOG` and nothing else: the setup wizard, the backend factory
  and `doctor` all read it, and this repository's catalog holds the IBM Bob entry
  (`BOB API KEY` label). Add a provider by adding an entry — never by naming it
  in the menu, a CLI default or an availability message. Endpoints and default model ids come
  from the vendor's own documentation (D-005) and carry its URL in the entry.
* The board/circuit layers (`schematic/`, `pipeline/demo.py`,
  `reporting/html.py`) belong to the earlier spec. They are dormant: the model path must not
  import them, and no public CLI or menu route may expose them. Internal model-alarm tests
  may reuse their helpers in small code-built circuits. D-052 supersedes board workflows.

## Layout rules

* `src/boardmodeler/` — package. `domain/records.py` is the shared data contract; changing it late
  invalidates fixtures and manifests, so change it deliberately and update `SCHEMA_VERSION`.
* `fixtures/` — committed test fixtures only. Anything downloaded from a vendor lives under
  `fixtures/**/originals/` and is **git-ignored** (local use only).
* `projects/`, `build/`, `runs/` — generated. Never committed.

## Hard rules

1. **Never write into the LTspice installation or library directory.** Decks are written under the
   project's `runs/`; models are referenced by absolute path at run time or copied beside the deck on
   export. `%LOCALAPPDATA%\LTspice\lib` is read-only for us.
2. **Never record an unobserved result.** A `TestResult` with `status=PASS` requires an observed
   simulator artifact (`.raw`/`.log`) with its hash recorded in the manifest. If the simulator is
   missing, the run is `BLOCKED`; missing measurements are `UNKNOWN`.
3. **Never present synthetic data as device data.** Everything under `fixtures/switch_fixture/` and
   `fixtures/demo_board/` carries `origin=TEST_FIXTURE` and `evidence.extraction=synthetic_fixture`.
   The synthetic PCIe-switch fixture has no real part number.
4. **Never report a citation as verified** unless its excerpt appears (whitespace-normalized) in the
   extracted text of the cited page. Absolute-maximum-ratings text is never an operating limit.
5. **Never silently fall back.** Provider selection is explicit: a configured provider is used or the
   stage is `BLOCKED` with the reason. Bob is never replaced by an external provider automatically.
6. **No telemetry.** Extraction and model artifacts are cached by hash; repeat runs make zero
   inference requests.
7. **Secrets stay inside this folder, and are plain local files.** The one API key is an ordinary
   local JSON file, `data/credentials.bob.json`, inside this extracted copy, or
   `BOARDMODELER_BOB_SHELL_API_KEY` / `BOB_API_KEY` for CI. Nothing is bound to Windows: no DPAPI
   ciphertext, no registry entry, no Credential Manager entry and no user-profile location, and no
   credential is written outside this folder. The tradeoff is deliberate and weaker at rest than
   DPAPI was: the file is **not encrypted**, so anyone who can read the folder can read the key —
   keep the copy private and do not share its `data` directory. Keys are never logged, written into
   project files, model output, the repository, setup or a ZIP, and are never exported.
8. **Repair is bounded and cannot weaken honesty.** Repair may only edit `models/candidates/<n>/`,
   is capped by `max_repair_iterations`, and may never relax a tolerance, delete a test, alter source
   evidence, narrow coverage, or edit the circuit.
9. **No temperature or statistical claims without modeled temperature dependence / explicit data.**

## Conventions

* Python 3.14, `from __future__ import annotations`, ruff line length 100.
* All record models: `model_config = ConfigDict(extra="forbid")`, JSON round-trip via
  `model_dump_json(indent=2)`.
* Tests: `pytest`, markers `ltspice`, `network`. A test earns its place only where a plausible
  bug would fail it.
* `docs/STATUS.md` is updated at every phase boundary with the exact commands run and their observed
  results. `docs/DECISIONS.md` records every decision that constrains later work.
