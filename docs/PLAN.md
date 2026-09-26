# PLAN

Historical implementation plan and phase gates. D-052 in
[`DECISIONS.md`](DECISIONS.md) supersedes every board checker, board import,
board demonstration, and findings-report deliverable below. The active
milestones are in [`SYSTEM_MODELS_PLAN.md`](SYSTEM_MODELS_PLAN.md).

## Product

Datasheet-grounded LTspice model generation, delivered as a `.lib`, `.asy`,
cited model card, and verification tests through the CLI and desktop app.
Model alarms run in the user's own LTspice simulation.

Honest statuses only: `PASS`, `FAIL`, `UNKNOWN`, `BLOCKED`, `NOT_APPLICABLE`.
Never a fabricated run, citation, or approval.

## Fixed decisions

* Python `>=3.14,<3.15` (D-001), hatchling, console script `boardmodeler`.
* Project workspace layout created per analysis run (never inside the LTspice
  installation): `project.json`, `docs/`, `evidence/`, `models/`, `circuit/`,
  `runs/<run_id>/`, `review.json`, `baseline.json`, `export/`.
* Record schemas in `domain/records.py` (D4) with `extra="forbid"` and
  `SCHEMA_VERSION`; constrained expression AST in `domain/expressions.py` (D5) —
  no `eval`, no codegen.
* Simulator adapter: single choke point (`simulation/ltspice.py`), resolved
  invocation per D-006; reader per D-007; backend selection per D-002.
* Model capability is probed per behavior key and never upgraded to
  `supported` without a probe; encrypted vendor models are
  `VENDOR_PIN_COMPATIBLE` / `VENDOR_MODEL_COMPARED`.
* Pipeline: `IDENTIFY → COLLECT_EVIDENCE → BUILD_REQUIREMENTS → REVIEW_SOURCES →
  FREEZE_BASELINE → SELECT_MODEL → COMPILE_SIMULATE → EVALUATE → REPAIR →
  EXPORT`; baseline frozen by hash before repair; repair only touches
  `models/candidates/<n>/`, capped at 3 iterations, and may never relax a
  tolerance, delete a test, alter evidence, narrow coverage, or edit the circuit.
* Providers: explicit selection, no silent fallback; remote inference requires
  `remote_inference_allowed` **and** `--allow-remote`; disclosure printed and
  stored before the first call; zero telemetry; hash-cached extraction.
* GUI is a thin client over the same pipeline (child process, line-delimited
  JSON), no duplicated verdict logic.

## Phases and gates

|Phase|Gate|
|---|---|
|0 Environment, contracts, simulator smoke test|`uv run pytest -q` green; `boardmodeler doctor --json` shows a real smoke test pass with the observed RC value; invocation/reader decisions recorded|
|1 Deterministic verification core|known-good and known-bad decks distinguished by real runs; primitive reference tests assert numeric trip points; every status in the enum produced by ≥1 test|
|2 First regulator|startup/EN/PG/current-limit tested with real runs; broken external feedback divider detected; unsupported behaviors visible as exclusions or explicit UNKNOWN|
|3 Integrated board-level demonstration|§15 demo works end to end through the CLI with real runs; ≥5 faults detected with refdes/net/time; BLOCKED and UNKNOWN cases present; export reruns from a fresh directory|
|4 Datasheet-to-model automation|a supplied document yields schema-valid, citation-verified requirements driving the existing verification path; invented citations rejected as UNKNOWN|
|5 Minimal GUI and packaging|workflow completes in the GUI without a terminal over the same core; frozen build finds LTspice and completes the demo|
|6 Broader device coverage|template-driven families with committed baselines; real PCIe-switch qualification stays BLOCKED until documentation is supplied|

## Verification (repository level)

```powershell
uv run pytest -q
uv run ruff check . ; uv run ruff format --check .
uv run boardmodeler doctor --json
uv run boardmodeler demo build --out build/demo
uv run boardmodeler circuit check --project build/demo --circuit build/demo/circuit/demo.asc --json
uv run boardmodeler run mutations --project build/demo --report build/mutation-report.json
uv run boardmodeler export --project build/demo --out build/demo-export
```
