# System-level models: milestone plan

Status date: 2026-09-25 (America/New_York). The target is a system-level LTspice
sanity model for I/O cards: exact external pins and required connections,
datasheet-bounded key values, and plausible switching and transient shapes.
`docs/TEMPLATE_CATALOG.md` defines the 22-family scope and build order.

## This run: M1, TI TPS54332 side-by-side evidence

1. Confirm both repositories' current commits and rules, the frozen TPS54332
   spec, TI reference deck/library, and explicitly selected LTspice executable.
2. Add a code-built experiment under `tools/` in the general edition. Render
   our switching library afresh from the frozen spec; use TI's reference-deck
   passives and package pin order for both models. Write decks and simulator
   artifacts under ignored `runs/`; never add the TI library to Git.
3. Run matched TI and local-model benches for a post-soft-start COMP sweep
   (50 mV steps, VSENSE held at 0.8 V, resistive load, at least three active
   points), resistive overload, about 10 mOhm short, startup, steady ripple and
   PH edges, and a 1 A to 3 A load step. Measure peak/average inductor current,
   frequency/foldback, gain fit, ripple, edge times, startup, and step response.
4. Record actual values, reference bands, PASS/FAIL/UNKNOWN reasons, elapsed
   LTspice times, and deck/log/raw SHA-256 in
   `docs/evidence/2026-09-25-system-models-m1/REPORT.md`. Keep raw files local
   only, then remove them after hashing. Do not edit `src/` or infer a PASS from
   missing simulator data.
5. Run focused checks for the experiment, lint, `git diff --check`, and shared
   core comparison. Update this tracker and `docs/STATUS.md`; then mirror the
   relevant documentation to Bob, run Bob's focused checks and compare again.
6. Push both `main` branches only after the repository checks pass. If the
   experiment cannot finish inside an hourly work period, split M1 into named
   parts in this tracker and report only completed evidence; never mark
   unfinished tests DONE. Continue to the next milestone after M1 is complete,
   with progress check-ins about every hour.

## This run: M2, deterministic pre-freeze guards and benches

M2 is split by deliverable because six waveform benches and the pre-freeze
validation need separate review. M2a is the first acceptance unit; M2b follows
without treating M2a as the whole milestone.

1. Confirm both current commits and frozen TPS54332 row citations. Inspect
   `authoring/test_planner.py`, `buck_fixtures.py`, `circuit_probe.py`, their
   focused tests, and the M1 waveform definitions before editing.
2. **M2a guards:** in the shared planner, reject a buck current-sense-gain
   fixture with fewer than two active COMP points, any fit point at or below
   cited VECO, or an unsupported/missing VECO citation. Reject an ideal current
   sink as a current-limit output load (either orientation) and a measurement
   window opening before cited soft-start charging plus settling. Use the latest
   VREF bound with the cited positive SS current; the TPS54332 2 µA value is
   typical, so do not call the calculated time a guaranteed silicon maximum.
   Add a clean control and a fault case for every rejection, including aliases.
3. **M2b benches:** build gain, min/max current-limit, ripple, PH edge, 1→3 A
   load-step, and startup decks deterministically from cited rows and explicit
   bench passives. Keep a 0.50 V diagnostic point out of the production gain
   fit; all production sweep points must exceed VECO. Reuse M1 waveform rules
   where valid, and correct the loose full-recovery interpretation. Generated
   decks alone never make an electrical PASS; real LTspice artifacts are needed.
4. Work in the general edition first. Run targeted guard/generator tests,
   existing `tests/authoring tests/models tests/pipeline tests/ltspice`, the
   frozen TPS54332 and LM358 regressions when model/harness behavior changes,
   Ruff and `git diff --check`. Copy shared production files to Bob only after
   general checks, run Bob's focused tests, and compare the 42-file shared core.
5. Record measured times, pass/fail/unknown outcomes, and any remaining M2b
   work in `docs/evidence/2026-09-25-system-models-m2/REPORT.md` and both
   status files. M2 activates no board-level suite case. Push both main branches
   only after focused checks pass; keep about-hourly check-ins.

## This run: M3, exposed-pad connection and frozen regression

M3 is split by the evidence it needs: **M3a** removes the internal pad-to-ground
tie and changes the contract to an external required connection; **M3b** adds a
cited static check and a real-simulation alarm, then reruns the frozen 19-row
TPS54332 regression. Neither subpart is DONE until its own proof is recorded.

1. Inspect the current nine-port template, contract, saved 19-row results, and
   the verified `B001_PIN_POWERPAD` requirement (PDF page index 2, printed p. 3).
   Keep the nine-pin subcircuit and symbol order byte-for-byte compatible.
2. **M3a:** replace `ground_tie_pins` in the contract with a required external
   connection to the GND role. Render POWERPAD and its aliases as distinct
   electrical pins: no 1 mΩ or other functional internal tie. A ≥1 GΩ
   convergence leak may remain only if needed and must never satisfy the
   required-connection rule. Test named and alias pads, no-pad parts, and
   wrong/missing pins; freshly render the frozen TPS model and inspect its netlist.
3. **M3b:** use the verified PowerPAD requirement to flag a disconnected or
   wrong-net pad on a code-built synthetic card, while a clean PCB tie produces
   zero findings. Add a simulator-observed alarm with a clean/fault pair and
   explicit pin, net, citation and waveform hash. If any alarm cannot be
   measured, keep it UNKNOWN and split the remaining work rather than count it
   as detection. Activate GND-02 only when both static and alarm evidence exist.
4. Rerun the exact frozen 19-row LTspice regression with the updated model and
   compare every verdict with the committed baseline. Explain each change,
   including a new UNKNOWN or FAIL, without relaxing a limit or editing the
   frozen spec. Keep decks and logs in ignored `runs/`; hash raw files before
   removal. Use an explicitly supplied LTspice path and no AI/network call.
5. Work in general first, then copy reviewed provider-neutral changes to Bob.
   Run targeted and existing model/harness tests, the frozen TPS and LM358
   regressions where applicable, Ruff, shared-core comparison, and diff checks.
   Update both status files and the M3 evidence report; push both branches only
   after tests pass. Send the next substantive check-in about one hour after M2.

## Milestones

`DONE` means its acceptance evidence exists. A partial milestone must be split
into named parts such as M3a/M3b rather than silently marked done. The commit
columns identify the substantive change in each repository; tracker follow-up
commits may record those IDs afterward.

| Milestone | Scope and acceptance | Status | Date | Evidence path | General commit | Bob commit |
| --- | --- | --- | --- | --- | --- | --- |
| M0 | D-048, 22-family catalog, plan; general evidence report covering both editions and status in both | DONE | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m0/REPORT.md` (general) | `13a578e8e7c2132029c47b33fa3e7df26c725911` | `c8c60c080d38d4de90b501aa90a24003df6819c7` |
| M1 | Fresh-rendered TPS54332 vs TI model with the same passives; gain slope at ≥3 active points, resistive overload and short, ripple, edges, startup, load step; evidence only | DONE (measured; FAIL/UNKNOWN retained) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m1/REPORT.md` (general) | `380988f131deba87bbf151a229cf4b989d9992ad` | `f3598591bb6c9312d84939b25e5a15873692189b` |
| M2 | Pre-freeze guard rules and code-built gain, limit, ripple, edge, load-step, startup benches | DONE (measured; edge UNKNOWN and M1 ripple FAIL retained) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m2/REPORT.md` (general) | `6bbc5717ec3c66a6f0ee7de9aeb53b087fa0e3b9` | `431af5b415bad1310457b2843b6c8b7a8b46716d` |
| M2a | Cited gain/current-limit pre-freeze guards with clean and fault controls | DONE | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m2/REPORT.md` (general) | `6bbc5717ec3c66a6f0ee7de9aeb53b087fa0e3b9` | `431af5b415bad1310457b2843b6c8b7a8b46716d` |
| M2b | Six deterministic bench families, waveform analysis, and measured LTspice proof | DONE (six MEASURED, edge UNKNOWN) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m2/REPORT.md` (general) | `6bbc5717ec3c66a6f0ee7de9aeb53b087fa0e3b9` | `431af5b415bad1310457b2843b6c8b7a8b46716d` |
| M3 | Remove unjustified ground/pad ties; required-connection check and internal alarm; explain frozen-row changes | DONE (synthetic slice; full Card A remains NOT BUILT) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m3/REPORT.md` (general) | pending tracker | pending tracker |
| M3a | Separate pad pin and external-connection contract, with render and alias tests | DONE | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m3/REPORT.md` (general) | pending tracker | pending tracker |
| M3b | Cited static/internal-alarm clean-fault controls, frozen 19-row TPS and LM358 regressions | DONE (synthetic slice; 12/4/3 TPS unchanged) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m3/REPORT.md` (general) | pending tracker | pending tracker |
| M4 | Matching SW and AVG peak-current buck modes; both-mode key values; SW shapes; speed; release checkpoint | TODO | — | — | — | — |
| M5 | Generic PinDefinition-based pin model and positive/negative tests for every alarm | TODO | — | — | — | — |
| M6 | Pinout evidence and confirmation gate in CLI/GUI; no system-verified status without confirmation | TODO | — | — | — | — |
| M7 | Default code-built system path; timed TPS54331/TPS54332/LM358 runs; release checkpoint | TODO | — | — | — | — |
| M8 | Mini I/O card, power-up and fault matrix; both seeded mistakes caught, clean card quiet | TODO | — | — | — | — |
| M9+ | One family per run in catalog order; contract, aliases, checklist, second-part evidence | TODO | — | — | — | — |

## Board-level system test suite

The owner's [system test suite](SYSTEM_TEST_SUITE.md) defines 66 fault cases on
four explicitly synthetic I/O cards. For every active case, a clean card must
produce no findings and a fault mutation must trigger the correct check at the
right part, pin or net with applicable source evidence. The 12 numeric `(+/-)`
cases also need inside/outside boundary pairs; corner runs use cited min/max
values. UNKNOWN is never counted as detection. Activate only the categories
assigned to each milestone in that document: M3/M5 static and pin checks,
M4/M8 regulator and card-A power checks, then M9+ family checks. The owner's
real board remains unavailable; no synthetic case is labeled a real-board test.

No electrical model row becomes `PASS` without a cited requirement and real
LTspice measurement. A static suite check instead needs inspected netlist and
pin data plus its clean and fault controls; it is labeled `S`, not a simulated
electrical pass.
Unknown or unsupported behaviors remain `UNKNOWN` or `NOT_APPLICABLE` with reasons.
The vendor TPS54332 library remains local and is never committed. The real
board's CAD source and two known defects are blocked pending owner input;
synthetic M5/M8 fixtures must be labeled synthetic.
