# Spice Maker 1.7.0 / Bob 1.6.0 plan — 2026-09-25

## Goals and acceptance gates

1. **Deliver a usable switching-regulator model promptly.** Classify supported buck
   parts from the frozen specification, render a known-convergent template with cited
   parameter origins, run the existing LTspice harness, and use the AI only to repair
   measured failures. A converging partial model is deliverable with honest FAIL,
   UNKNOWN and template-default labels; no result is called PASS without simulator
   evidence. Record the time from GO to deliverable and compare with the 1957 s
   TPS54332DDA run that delivered no model.
2. **Make test plans physically valid.** Reject or defer invalid AI-planned fixtures
   before freezing the specification, with a reviewable reason. Compare current
   magnitude unless the datasheet establishes a polarity. Keep every cited limit and
   measurement window unchanged.
3. **Prove model quality.** Judge the TPS54332DDA template against the frozen 19
   fixtures and record every row's verdict and timing. Run a full datasheet build,
   one different regulator, and an LM358 regression. List physical reasons for any
   fixture a faithful model cannot pass.
4. **Keep both editions usable and in step.** Fix the SETUP hint overlap, review
   rendered symbols, synchronize and compare the shared core, preserve the Bob-only
   rules and the general edition's lack of Bob provider access.
5. **Publish only observed working releases.** Run both full test suites with the
   explicitly selected LTspice, lint, credential scan, and installer checks. Rebuild
   both `Install.exe` files and verify each GitHub Download ZIP → hash → install →
   start path. Commit and push both `main` branches only after those gates pass.

## Work order

1. Inspect the existing pipeline and frozen evidence; implement the template,
   fixture checks, current comparison and SETUP layout in parallel.
2. Integrate the template into the build loop, run the focused simulator fixtures,
   and correct only observed failures.
3. Copy shared code to Bob, run both suites and safety checks, and update version,
   README, status and decision records.
4. Build both installers, verify locally, then push and re-check the download path.

Progress is reported within one hour of the start of this work block. If release
gates cannot be met in the available time, report the exact passing and failing
evidence instead of publishing an unverified installer.
