# AGENTS.md — repository rules

## Commands

```powershell
uv sync --all-extras                       # single setup command
uv run pytest -q                           # unit + integration (LTspice-marked tests run locally)
uv run pytest -q -m "not ltspice"          # simulator-free subset
uv run ruff check . ; uv run ruff format --check .
uv run boardmodeler doctor --json

# the product: datasheet -> agent-authored, simulator-judged model
uv run boardmodeler ui                     # the model maker window
uv run boardmodeler model build --part TPS54320 --datasheet <pdf> --out build/tps54320
uv run boardmodeler model test  --out build/tps54320
uv run boardmodeler model install --out build/tps54320 --user-lib --apply
```

## The model-maker path (D-014)

The GUI defaults to `authoring/sanity.py`: no AI test planning, one bounded unpowered load per candidate when LTspice is available, at most two author turns, and UNKNOWN electrical status. Full verification remains optional and its rules below still apply. Never describe a structural check as measured accuracy.

* `authoring/` is the engine: `spec.py` (the frozen datasheet rows), `probes.py` (one deck
  per characteristic), `harness.py` (run + judge), `backends.py` + `loop.py` (the agent),
  `card.py` (deliverables). `pipeline/make_model.py` chains them; `ui/model_maker.py` and
  `boardmodeler model …` are two faces of that one chain.
* The **spec is frozen**: `spec/characteristics.json` is hashed before the agent starts and
  re-checked after every turn. A changed spec stops the build as `UNKNOWN(spec_tampered)`;
  the agent may write only `model/<SUBCKT>.lib` and `model/<SUBCKT>.asy`.
* A row that a probe cannot answer is `UNKNOWN` with its reason; a datasheet row no probe can
  reach keeps a written `not_testable_reason` and appears on the card. Never stretch a probe
  to cover a row it does not exercise, and never relax a limit to make a model pass.
* The UI has two surfaces and they must not grow: `ui/model_maker.py` is the build
  (part number, datasheet, save location, GO, progress, results) and `ui/setup_dialog.py` is
  everything persistent (LTspice path, agent key, model folder, LTspice library, web
  reinforcement). Anything that is asked once per machine belongs in setup; anything asked
  per build belongs in the window. Both are content-sized — never add fixed-height frames with
  empty space, and never add a background rule that can repaint the buttons (see the pixel
  test in `tests/gui/test_window_contract.py`).
* The agent is reached with an **API key, never a login** (D-015). Which providers a build
  accepts is `agent_providers.CATALOG` and nothing else: the setup page, the backend factory
  and `doctor` all read it, and this repository's catalog holds the IBM Bob entry (no provider
  row on SETUP, `BOB API KEY` label). Add a provider by adding an entry — never by naming it
  in a window, a CLI default or an availability message. Endpoints and default model ids come
  from the vendor's own documentation (D-005) and carry its URL in the entry.
* The board/circuit layers (`schematic/`, `pipeline/demo.py`, `ui/main_window.py`,
  `reporting/html.py`) belong to the earlier spec. They are dormant: the model path must not
  import them, and they are reached only by explicit flags.

## Layout rules

* `src/boardmodeler/` — package. `domain/records.py` is the shared data contract; changing it late
  invalidates fixtures and manifests, so change it deliberately and update `SCHEMA_VERSION`.
* `fixtures/` — committed test fixtures only. Anything downloaded from a vendor lives under
  `fixtures/**/originals/` and is **git-ignored** (local use only).
* `projects/`, `build/`, `runs/` — generated. Never committed.

## Hard rules

1. **Never write into the LTspice installation or library directory.** Decks are written under the
   project's `runs/`; models are referenced by absolute path at run time or copied beside the deck on
   export. `C:\Users\<user>\AppData\Local\LTspice\lib` is read-only for us.
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
7. **No plaintext secret persistence.** Credentials live as current-user DPAPI ciphertext in this extracted copy's `data/credentials.bin` file or
   `BOARDMODELER_<NAME>_API_KEY` for CI; they are never logged, written into project files, or exported.
8. **Repair is bounded and cannot weaken honesty.** Repair may only edit `models/candidates/<n>/`,
   is capped by `max_repair_iterations`, and may never relax a tolerance, delete a test, alter source
   evidence, narrow coverage, or edit the circuit.
9. **No temperature or statistical claims without modeled temperature dependence / explicit data.**

## Conventions

* Python 3.14, `from __future__ import annotations`, ruff line length 100.
* All record models: `model_config = ConfigDict(extra="forbid")`, JSON round-trip via
  `model_dump_json(indent=2)`.
* Tests: `pytest`, markers `ltspice`, `network`, `gui`. A test earns its place only where a plausible
  bug would fail it.
* `docs/STATUS.md` is updated at every phase boundary with the exact commands run and their observed
  results. `docs/DECISIONS.md` records every decision that constrains later work.
