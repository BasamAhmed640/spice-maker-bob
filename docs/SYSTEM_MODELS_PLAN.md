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

## Milestones

`DONE` means its acceptance evidence exists. A partial milestone must be split
into named parts such as M3a/M3b rather than silently marked done. The commit
columns identify the substantive change in each repository; tracker follow-up
commits may record those IDs afterward.

| Milestone | Scope and acceptance | Status | Date | Evidence path | General commit | Bob commit |
| --- | --- | --- | --- | --- | --- | --- |
| M0 | D-048, 22-family catalog, plan; general evidence report covering both editions and status in both | DONE | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m0/REPORT.md` (general) | `13a578e8e7c2132029c47b33fa3e7df26c725911` | `c8c60c080d38d4de90b501aa90a24003df6819c7` |
| M1 | Fresh-rendered TPS54332 vs TI model with the same passives; gain slope at ≥3 active points, resistive overload and short, ripple, edges, startup, load step; evidence only | DONE (measured; FAIL/UNKNOWN retained) | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m1/REPORT.md` (general) | `380988f131deba87bbf151a229cf4b989d9992ad` | `f3598591bb6c9312d84939b25e5a15873692189b` |
| M2 | Pre-freeze guard rules and code-built gain, limit, ripple, edge, load-step, startup benches | TODO | — | — | — | — |
| M3 | Remove unjustified ground/pad ties; required-connection check and alarm; explain frozen-row changes | TODO | — | — | — | — |
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
