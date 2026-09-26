# System-level models: milestone plan

Status date: 2026-09-25 (America/New_York). The target is a system-level LTspice
sanity model for I/O cards: exact external pins and required connections,
datasheet-bounded key values, and plausible switching and transient shapes.
`docs/TEMPLATE_CATALOG.md` defines the 22-family scope and build order.

## This run: M0, documentation only

1. Read both repositories' current rules, decisions, status, buck-slice evidence,
   and template catalog. Confirm the lowest unfinished milestone.
2. In the general edition, add decision D-048; replace the catalog with the
   22-family scope and order; create this tracker and an M0 evidence report;
   update `docs/STATUS.md`. Do not edit `src/` or an installer.
3. Review the general docs against the owner's prompt, including pin-source
   gating, must-be-right and ballpark bands, SW/AVG behavior, code-built tests,
   safety, and truthful current implementation status.
4. Mirror and adapt the documentation in the Bob edition without claiming a
   Bob live model test. Keep both editions' milestone descriptions aligned.
5. Verify: `git diff --check` in both repos; catalog family numbers 1–22 and
   build order 1–10; D-048 and tracker content; focused
   `tests/test_shared_core.py` in both repos; byte equality of the common
   catalog and tracker. Record exact results in the evidence report and both
   status files.
6. Commit and push both `main` branches only after the checks pass. Enter the
   substantive documentation commit IDs in this tracker and report them at
   check-in. Stop after M0; M1 is the next run.

## Milestones

`DONE` means its acceptance evidence exists. A partial milestone must be split
into named parts such as M3a/M3b rather than silently marked done. The commit
columns identify the substantive change in each repository; tracker follow-up
commits may record those IDs afterward.

| Milestone | Scope and acceptance | Status | Date | Evidence path | General commit | Bob commit |
| --- | --- | --- | --- | --- | --- | --- |
| M0 | D-048, 22-family catalog, plan; general evidence report covering both editions and status in both | IN_PROGRESS | 2026-09-25 | `docs/evidence/2026-09-25-system-models-m0/REPORT.md` (general) | pending | pending |
| M1 | Fresh-rendered TPS54332 vs TI model with the same passives; gain slope at ≥3 active points, resistive overload and short, ripple, edges, startup, load step; evidence only | TODO | — | — | — | — |
| M2 | Pre-freeze guard rules and code-built gain, limit, ripple, edge, load-step, startup benches | TODO | — | — | — | — |
| M3 | Remove unjustified ground/pad ties; required-connection check and alarm; explain frozen-row changes | TODO | — | — | — | — |
| M4 | Matching SW and AVG peak-current buck modes; both-mode key values; SW shapes; speed; release checkpoint | TODO | — | — | — | — |
| M5 | Generic PinDefinition-based pin model and positive/negative tests for every alarm | TODO | — | — | — | — |
| M6 | Pinout evidence and confirmation gate in CLI/GUI; no system-verified status without confirmation | TODO | — | — | — | — |
| M7 | Default code-built system path; timed TPS54331/TPS54332/LM358 runs; release checkpoint | TODO | — | — | — | — |
| M8 | Mini I/O card, power-up and fault matrix; both seeded mistakes caught, clean card quiet | TODO | — | — | — | — |
| M9+ | One family per run in catalog order; contract, aliases, checklist, second-part evidence | TODO | — | — | — | — |

No row becomes `PASS` without a cited requirement and real LTspice measurement.
Unknown or unsupported behaviors remain `UNKNOWN` or `NOT_APPLICABLE` with reasons.
The vendor TPS54332 library remains local and is never committed. The real
board's CAD source and two known defects are blocked pending owner input;
synthetic M5/M8 fixtures must be labeled synthetic.
