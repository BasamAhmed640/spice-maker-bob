# Engine guide

The engine is an evidence-to-model compiler. This is the implementation contract recovered
from the owner's engine review page (baseline b1ced1c, 2026-09-28). The saved original page is
unchanged; a byte-identical [original review page](engine-refactor.html) is preserved here for RAG
and offline reading. The owner explicitly authorized the later PWM family, GUI and delivered-installer work.

## Default build

The owner's 2026-09-30 clarification takes precedence over the historical page:
the ordinary workflow asks for a datasheet and part number, resolves routing in
code, and checks official manufacturer model downloads first. If an unchanged
official model actually loads in LTspice, deliver that original rather than a
generated approximation. Preserve its bytes, filenames, dependencies, entry
point, license and declared scope; any adaptation must be a separate labelled
artifact. Internet component verification is restricted to official manufacturer
documentation. Provider sites, distributors and manufacturer community posts are
not component specifications.

Version 1.8.1 implements bounded product-page discovery for TI and Analog Devices,
safe original acquisition and an observed compatibility check before the old
generation gate. This is a first delivery milestone, not universal coverage.
Official originals currently remain **UNKNOWN for datasheet electrical accuracy**:
one unpowered operating-point check establishes loading only, and generated
symbols describe model ports rather than verified physical package pin numbers.
The following evidence/compiler/testing rules still govern generated models.

1. Read the selected datasheet and record facts with exact page citations and document hashes.
   AI may extract evidence. Exact reviewed inputs or validated caches can avoid this AI step.
2. Resolve the component family and physical package. Refuse unsupported classes, ambiguous
   pin assignments and missing required evidence before expensive work.
3. Create a versioned, typed design containing the part, package, physical pins, parameter
   values, units, origins, renderer version and explicit behavior limits.
4. Render the library deterministically in reviewed code. The same facts and renderer version
   produce identical design serialization and library bytes.
5. Build and freeze tests independently from verified source evidence and reviewed benches.
   The candidate supplies the device under test; it never supplies its acceptance limits.
6. Run the exact candidate in LTspice. Retain every measured PASS, FAIL, UNKNOWN and coverage gap.
7. Publish only when the source-backed pin map, subcircuit ports and symbol SpiceOrder agree.
   Bind the design, symbol, source, frozen tests and simulator evidence to the delivered bytes.

AI test planning, free-form authoring and repair are explicit legacy options. They cannot become
a silent fallback when a code builder or evidence is missing. A separately requested pin-only
shell has no component-function claim.

## Evidence and safety

- A source excerpt must match the cited page before it can supply a model parameter or test bound.
  Absolute maximum ratings are stress limits, not normal operating targets.
- Tests include the cited operating conditions, stimuli, external connections and measurement
  windows. Changing a model parameter must leave the frozen acceptance criterion unchanged.
- No electrical PASS without an observed simulator artifact and its hash. A simulator exit code,
  a template default, syntax success or a matching pinout does not establish electrical accuracy.
- Required external connections stay external. Package confirmation is not a PCB-footprint claim.
- MCU, FPGA, CPLD, processor and SoC classes are excluded on every route.
- Missing physics is fixed in reviewed code and independently measured. Runtime AI tuning is
  outside the default route. Temperature/statistical behavior needs explicit modeled evidence.
- Preserve old saved outputs and their historical provenance. Legacy bytes changed by AI cannot
  keep an exact design association; they retain only their honestly labelled prior provenance.
- Do not write into LTspice's installation or library directories. No telemetry or logged keys.

## Support and timing

An architecture that can add families is not proof that most PCB parts are implemented.
A supported claim requires a matching implementation, cited essential inputs and independent
tests for essential behaviors. Report the current support list and narrower model scope plainly.
A passing subset does not qualify the entire device family.

Keep a per-run timing record with extraction, compilation, simulation, total time and observable
provider call counts. Bound provider stages and each simulation. The 5-10 minute objective is
a measured target, not an unconditional promise. Unsupported builds should stop before paid AI
work when the existing publication policy already makes delivery impossible.

## Verification and delivery

Validate canonical serialization, invalid inputs, source/package mutations and final artifact
identity. Use real wrong-value and wrong-state controls against unchanged tests when changing
physics or qualification. Reproduce the frozen TPS54332 and LM358 controls when relevant and
explain changed verdicts. Keep FAIL/UNKNOWN results visible.

Run the affected software suites, lint/format checks, shared-core parity checks and diff checks.
Record actual commands/results in STATUS and decisions in DECISIONS. Shared renderer JSON and
new code must participate in cache invalidation and cross-edition fingerprints.

A release must exercise the installed wheel and frozen desktop executable, compare their engine
identity with the release source, and build a measured model through that installed application.
Passing tests against an adjacent source checkout does not verify an old installer.

## Boundaries

The destination architecture must cover ordinary PCB components through reusable
electrical behavior blocks and source-derived design records, rather than a
part-number/PDF-hash whitelist. Family and package choices are internal routing
details. A genuinely ambiguous physical pin map requires one focused clarification.
Broad source extraction, behavior composition and independent multi-manufacturer
electrical qualification are still required; a vendor download or a pin shell
must never be reported as completing that work.

Build component models, not board-checking workflows. Add genuinely different families
incrementally with their own evidence and tests; do not relabel a generic pin shell as a working
component. The owner's subsequent GUI request keeps the existing two-window desktop product and
its timer; it does not change any engine acceptance rule.

Implementation evidence: [current status](STATUS.md), [decisions](DECISIONS.md),
[engine refactor measurements](evidence/2026-09-29-engine-refactor/REPORT.md),
[source-backed publication](evidence/2026-09-29-m6-pinout/REPORT.md).
