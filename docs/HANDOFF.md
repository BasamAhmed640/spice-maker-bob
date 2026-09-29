# Hand-off — engine run of 2026-09-28/29

Written because the owner asked for a stop and a hand-off at 90 percent of the 5-hour usage limit.
Read this first, then `docs/STATUS.md` (newest entry on top) and `docs/DECISIONS.md` (D-054 to D-057).

## Where things stand

- Both repos are on `main` and pushed; the automated checks (ruff check, ruff format, the full pytest
  step) pass on the tips. The 46-file shared core is identical across the two editions
  (`tools/shared_core.py --check`, and `--compare ../spice-maker` from the Bob edition).
- The engine is an evidence-to-model compiler. The AI extracts cited rows; code builds a typed design
  and renders the SPICE; LTspice judges. Three routes, none falls back to another: `legacy_ai` (the
  default, agent authoring), `behavioral` (code-built, only for a part the support decision marks
  supported), `pin_only` (limited, asked for by name, never a pass).
- Support needs a positively matched implementation, every essential input cited, and every essential
  behaviour covered by an independent bound row. Microcontrollers, FPGAs, CPLDs, processors and SoCs are
  refused on every route. `support-decision.json` is written for every run.
- Built by code today: the peak-current buck (TPS54332) and the dual op amp (LM358), one part each.

## Live runs made on 2026-09-29 (general edition build, one key, opencode_go)

| Part | Route | Result | Time |
| --- | --- | --- | --- |
| TPS54331 (second buck, cached extraction) | behavioral | refused, `unsupported_family`: no cited current limit or enable threshold, no independent rows for four behaviours | 9 s |
| XD7660 (charge pump) | pin_only | UNKNOWN by design, gate 12 pass, 1 unknown, 0 fail | 158 s (extract 154.5 s) |
| LM5116 (21-pin controller) | pin_only | BLOCKED: gate failed two checks, nothing delivered | 348 s (extract 342.4 s) |
| TPS54331 with `--plan-tests` | behavioral | see the last section | see the last section |

Outputs, including `run-timing.json` and `support-decision.json` per run, are under
`C:\Users\basam\src\.smsnap\ex\` (`tps54331`, `xd7660`, `lm5116`, `tps54331_plan`, and `summary.log`).
The runner scripts there (`run_ex.sh`, `run_plan.sh`) show the exact command: a temporary config via
`BOARDMODELER_CONFIG` (the real one is untouched), a frozen copy of `src` on `PYTHONPATH`, one build
at a time because there is one key.

## Do next, in this order

1. Read the TPS54331 `--plan-tests` result (last section). If the decision is still not supported, the
   missing pieces are the cited current limit and enable threshold; the extraction has to cite them.
2. Save the failing gate report when `pin_only` is blocked (today `pin-only-report.json` is written only
   on success, so the LM5116 cause is not inspectable). Then decide whether a table with several
   grounds or rails should be refused before the build, as two supply domains already are.
3. Count provider calls in `run-timing.json` (an acceptance line of the north-star page).
4. More families need an implementation, cited inputs and independent tests each: regulators (LDO),
   references, comparators, logic, and a single or quad op amp (the probes fix the eight-pin dual).
5. Expose `--engine` and `--family` in the window; then the separate terminal-app refactor
   (Setup.cmd/bootstrap in place of the Qt window and Install.exe).
6. Local branches `engine/*` and `pin-model` are merged and can go with the owner's OK; keep
   `wip/m4b2` parked and unmerged; leave `spice-maker-next` and `spice-maker-bob-next` alone.

## Rules that stay in force

- No PASS without an observed LTspice result; never relax a limit; keys are never printed or logged
  (environment variable names only); the app writes only inside its folder; LTspice is never searched
  for; no telemetry; the TI PSpice model stays local; vendor PDFs are never committed.
- Push to `main`. Report to the owner in plain words (no plan labels, no check names).
- Run tests with the repo venv: `.venv/Scripts/python.exe -m pytest -q -m "not ltspice and not network"`
  (this is the automated check; add `QT_QPA_PLATFORM=offscreen` for the window tests). Frozen specs under
  `models/` are git-ignored, so tests that need them skip on a clean checkout.
- A tool that pins the environment does it in its entry point, never at import (`tests/conftest.py`
  refuses a run that breaks this).
- Shell tips on this machine: Bash heredocs mangle backslashes and choke on apostrophes in big blocks;
  put code in small files; there is no `pkill` (use PowerShell `Stop-Process`).

## The TPS54331 `--plan-tests` run

Pending when this was written: the run started at 00:48:50 and is capped at 20 minutes. Its output is in
`C:\Users\basam\src\.smsnap\ex\tps54331_plan\` (look at `support-decision.json`, `run-timing.json`,
`results.json`) and the last lines of `summary.log`. The earlier attempt with planning ran past 11
minutes and was stopped; that is why planning is opt-in.
