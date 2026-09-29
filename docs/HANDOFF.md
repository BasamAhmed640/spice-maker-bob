# Hand-off — engine run of 2026-09-28/29, audited 2026-09-29

Written after the prior run parked at 84 percent, short of the owner's 90 percent usage line.
Read this first, then `docs/STATUS.md` (newest entry on top) and `docs/DECISIONS.md` (D-054 to D-058).

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

## Takeover audit and correction (2026-09-29)

- The saved LM5116 requirements and bindings replay offline. The old run's failed gate report was
  lost because only successful publication wrote it. The report now survives a withheld model, while
  the library, symbol and card remain absent. A replay with real LTspice 26 took 9.954 s, called no
  provider, and retained the current-conservation and quiet-alarm measurements. The shell cannot
  honestly handle several required supply pins: the saved table has VIN, VCC and HB, and it treated
  correctly powered VCC/HB as missing ties. Such a pin table is now refused before simulation with
  `pin_only_required_secondary_supply`; an optional auxiliary pin does not trigger that refusal.
- `run-timing.json` now counts inference HTTP attempts at the transport call, including retries. A
  local route with supplied evidence records zero. Bob Shell does not expose its internal vendor
  requests: after a shell invocation the total is `null` with a reason, and its shell invocation
  count is recorded separately. A run refused before authoring has route
  `refused_before_authoring`, rather than `agent_authoring`.
- The TPS54331 refusal was described incorrectly below. The cited source has an EN typical threshold
  of 1.25 V and current-limit typical value of 5.8 A; the saved extraction misassigns table columns.
  It also calls the VIN UVLO maximum of 3.5 V a typical value, though the datasheet says its typical
  VIN UVLO threshold is unspecified. The missing `UVTH` input is VIN UVLO, **not** EN. A hyphen in
  `Current-limit` also hid the row from the matcher. The old run records are historical evidence,
  not values to edit in place. See the [TI TPS54331 Rev. H datasheet](https://www.ti.com/lit/ds/symlink/tps54331.pdf)
  (electrical characteristics and operating description). With the bounded hyphen matcher fixed, an
  offline replay takes 6.685 s and remains BLOCKED for `UVTH` and missing independent VREF and
  current-limit tests; it makes zero provider calls and writes no model.
- This remains a two-part demonstration of behavioral code building, not universal coverage of PCB
  components. The positive support gate and the MCU/FPGA exclusions remain essential.

## Live runs made on 2026-09-29 (general edition build, one key, opencode_go)

| Part | Route | Result | Time |
| --- | --- | --- | --- |
| TPS54331 (second buck, cached extraction) | behavioral | refused, `unsupported_family`: recorded decision lacks ILIM/UVTH and four independent behaviours; the audit above corrects the interpretation | 9 s |
| XD7660 (charge pump) | pin_only | UNKNOWN by design, gate 12 pass, 1 unknown, 0 fail | 158 s (extract 154.5 s) |
| LM5116 (21-pin controller) | pin_only | BLOCKED: gate failed two checks, nothing delivered | 348 s (extract 342.4 s) |
| TPS54331 with `--plan-tests` | behavioral | refused again, `unsupported_family`: recorded decision lacks ILIM/UVTH and independent VREF/current-limit tests (10 of 112 rows bind) | 294 s (planning call 284 s) |

Outputs, including `run-timing.json` and `support-decision.json` per run, are under
`C:\Users\basam\src\.smsnap\ex\` (`tps54331`, `xd7660`, `lm5116`, `tps54331_plan`, and `summary.log`).
The runner scripts there (`run_ex.sh`, `run_plan.sh`) show the exact command: a temporary config via
`BOARDMODELER_CONFIG` (the real one is untouched), a frozen copy of `src` on `PYTHONPATH`, one build
at a time because there is one key.

## Do next, in this order

1. For TPS54331, make a new reviewed extraction revision pinned to the exact document hash and
   correct the MIN/TYP/MAX cell assignments. Do not relabel a maximum as a nominal value, and do not
   overwrite the historical run. Use deterministic, physically loaded VREF/current-limit benches
   with the existing soft-start guard; the optional AI planner's early measurement windows are not
   valid substitutes. The current `UVTH` contract has no cited typical value for this datasheet.
   Keep the part blocked until an explicit, tested corner policy or other honest design contract
   resolves that gap. No broad AI replanning call is needed to establish these facts.
2. Fix the pin-shell clean/open supply controls for multi-rail devices as a separate engineering
   task if that scope is added. Keep the structural refusal until then; a green gate must not be
   obtained by treating an unpowered rail as a valid test of a powered controller.
3. More families need an implementation, cited inputs and independent tests each: regulators (LDO),
   references, comparators, logic, and a single or quad op amp (the probes fix the eight-pin dual).
4. Expose `--engine` and `--family` in the window; then the separate terminal-app refactor
   (Setup.cmd/bootstrap in place of the Qt window and Install.exe).
5. Local branches `engine/*` and `pin-model` are merged and can go with the owner's OK; keep
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

Started 00:48:50, finished in 293.7 s (bind 284.2 s is the planning call, extract 6.7 s from the cache).
Result: BLOCKED, `unsupported_family`, missing: cited input ILIM, cited input UVTH, independent test
for reference voltage, independent test for current limit. 10 rows judged by probes, 102 declared not
testable. The earlier attempt with planning ran past 11 minutes and was stopped; that is why planning
is opt-in. Output: `C:/Users/basam/src/.smsnap/ex/tps54331_plan/`.
