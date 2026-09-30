## 2026-09-30 — the terminal setup lands on main (D-061)

The terminal setup written on 2026-09-28 (branch `terminal-app`) is merged into `main`. `Setup.cmd`
makes a `.venv` in the extracted folder and installs hash-checked packages, `Start.cmd` opens a text
menu, and `Boardmodeler.cmd` runs the flag commands. The Qt window, the compiled installer
(`Install.exe`, `installer/`), their two release workflows and the window tests are removed from the
tree; `Install.exe` stays in git history only. Nothing in the model engine changed.

| Check | Result |
| --- | --- |
| Conflicts | 12 files (six documents, six files the terminal branch deleted); all in documentation or in files the terminal branch deleted. Resolved by keeping the branch's deletions and setup documents and this line's engine documentation, with the window wording rewritten for the menu |
| Full automated tests on the merged tree (`pytest -m "not ltspice and not network"`) | 1919 passed, 20 skipped, 186 deselected in 77 s (four existing slow-marker warnings) |
| `ruff check`, `ruff format --check` | clean |
| Shared core | 49 files intact and identical across the two editions |
| Workflows | `ci.yml` unchanged in effect; `verify-python-installers.yml` runs only by hand or on pushes to `terminal-app`, with read-only permissions |

Things to know.

- The window had gained an engine and family picker (session of 2026-09-29). It went with the window.
  The menu builds with the default engine, `behavioral`; use
  `Boardmodeler.cmd model build --engine legacy_ai|pin_only` and `--family` for the others. A menu prompt
  for the engine is not built yet.
- The terminal branch numbered its decision D-053, which collided with the pin-model decision of the same
  number, so it is D-061 here. The branch's shortened STATUS and DECISIONS were not adopted: main keeps its full
  logs, and the branch's own history copies are under `docs/evidence/2026-09-28-terminal/history/`.
- The terminal branch's own verification (real setup from a source ZIP, LTspice smoke test, Python installer
  checks on hosted runners) is recorded in `docs/evidence/2026-09-28-terminal/REPORT.md`; it was not repeated
  for this merge, which is checked by the automated tests and the hosted run of `ci.yml`.
- Installs made from the old `Install.exe` are unaffected until their owner replaces the folder with a new ZIP.

## 2026-09-29 — support-gate identity and bench eligibility safeguards (D-060)

Two reproduced support-gate bypasses are closed. First-page device identity is checked in the
Features, Description and Overview sections as well as the heading, so an unlisted MCU,
FPGA/CPLD, processor or SoC cannot become an allowed model merely because its class appears
after a section heading. Application-only references to devices being powered remain distinct
from the identity of the requested part. Declared family hints cannot override a blocked class.

Buck support now requires each essential behavior to have a numeric operating reference and a
valid `circuit_measurement` recipe with the exact physical terminal set, matching units and a
compatible measurement operation.
An unrelated generic probe, an empty or invalid recipe, missing condition evidence, or a
dimensionally mismatched measurement no longer satisfies coverage merely because the row's
statement names the behavior. Refused behavioral requests retain their missing-test reasons.
These are support eligibility checks; only subsequent frozen LTspice observations can earn
electrical PASS. M6 package confirmation and the unfinished buck qualification checks remain open.

Focused command: `pytest -q tests/models/test_support.py tests/pipeline/test_support_gate.py
-m "not ltspice and not network"`, with the work checkout's `src` on `PYTHONPATH` and its
edition's canonical venv. General: **284 passed, 1 skipped, 2 deselected in 6.78 s**. Bob:
**283 passed, 1 skipped, 2 deselected in 7.42 s**, with the existing `slow`-marker warning.
Changed-file Ruff check/format, `git diff --check`, and the 49-file shared-core checks passed.
Support decisions replayed against the saved TPS54332 and LM358 fixtures remain supported;
the variants assigning one unrelated probe to all essential rows are refused. This replay
does not rerun LTspice or alter the existing four nominal PASS / twelve mandatory UNKNOWN
buck qualification results. No new electrical qualification is claimed by this safety follow-up.

Full offline command: `pytest -q -m "not ltspice and not network"`, using the same checkout
and venv setup. General: **2241 passed, 20 skipped, 190 deselected in 101.79 s**. Bob:
**1999 passed, 31 skipped, 187 deselected in 75.69 s**, with four existing `slow`-marker warnings.

A subsequent **General-only live LTspice TPS54332DDA replay** retained 12 PASS / 4 FAIL /
41 UNKNOWN / 49 N/A ordinary rows and 4 PASS / 12 UNKNOWN supplemental checks, with
`family_qualified=false` and zero provider calls. Extract / compile / simulate / total time was
**1.924 / 0.004 / 159.805 / 164.873 s**. The delivered library SHA-256 remains
`21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2`; the frozen plan SHA-256
remains `19530176920a72d67c1e9531138c5c20bd1e2d91dc123db4453a023d8c3eb267`.
Bob received the saved-fixture regression and full offline suite above, with **no new live
simulator run for this safety follow-up**. These unchanged results add no broader qualification.

## 2026-09-29 — frozen qualification and reviewed source evidence (D-059)

GitHub-reviewable acceptance summary:
[`docs/evidence/2026-09-29-engine-refactor/REPORT.md`](evidence/2026-09-29-engine-refactor/REPORT.md).

The buck qualification plan is now production code, frozen from the source requirements and spec
before candidate authoring. It contains the source hashes, cited conditions, independent benches,
mandatory checks and implementation hashes. Candidate parameters cannot change its references or
acceptance bands. The ordinary row harness and the supplemental qualification report remain separate;
four measured nominal passes do not qualify the whole buck family.

The broader north-star acceptance is still incomplete. **M6 remains open**: package selection,
symbol pin numbers and discrete terminal order need source-backed confirmation before publication
(PIN-01/02/05). Current file identity checks, recorded pin order and the TPS54331 PowerPAD guard
do not complete that gate.

Citation replay now persists the current page-check result on every DOCUMENT row before writing
new frozen requirements or a qualification plan. Missing document text, failed excerpts and missing
verifier results clear stale `citation_verified=true` flags. The input history stays untouched.
Regressions in `tests/pipeline/test_frozen_citation_state.py` cover false current-IQ source flags,
missing source text and missing verifier results; affected qualification cases stay gaps.
Resumed buck/op-amp designs are reconstructed and rendered before retaining an exact association.
Schema/type, design/library hashes, part, subcircuit and frozen spec must match; an edited payload
cannot keep an exact claim merely because library bytes are unchanged. Invalid provenance supplies
no exact design hash to qualification. Supplemental BLOCKED is publicly BLOCKED with its refusal
reason; fixed FAIL stays FAIL, while fixed UNKNOWN downgrades an ordinary PASS.

The window, CLI and new `MakeModelRequest` API now default to `behavioral`. AI extraction is
the only default AI stage and is avoided when supplied, cached or exact reviewed evidence suffices.
AI authoring and repair require the explicit legacy route. AI planning runs with full legacy
verification or a separate local-route opt-in. Behavioral and pin-only routes require full verification; quick drafts require
`legacy_ai`. Old saved requests without an engine keep their historical legacy interpretation.

Final default-route replay receipts after citation/provenance/status hardening are retained in each checkout's
`runs/engine-acceptance-opamp/`, `runs/engine-acceptance-buck/` and
`runs/engine-acceptance-tps54331/`, including `results.json`, `run-timing.json`, support decisions,
and the delivered hashes/reports where a model was published. All six runs recorded zero provider
calls with complete accounting; none exercised live inference.

| Default-route case | Row result | Extract / compile / simulate / total (s) |
| --- | --- | --- |
| General LM358, exact reviewed PDF | PASS; 32 PASS / 10 N/A | 5.572 / 0.003 / 15.647 / 26.237 |
| Bob LM358, exact reviewed PDF | PASS; 32 PASS / 10 N/A | 5.513 / 0.001 / 15.648 / 26.158 |
| General TPS54332DDA | UNKNOWN; 12 PASS / 4 FAIL / 41 UNKNOWN / 49 N/A | 2.013 / 0.004 / 156.473 / 161.600 |
| Bob TPS54332DDA | UNKNOWN; 12 PASS / 4 FAIL / 41 UNKNOWN / 49 N/A | 2.104 / 0.009 / 158.944 / 164.024 |
| General TPS54331 | BLOCKED before authoring; no library or symbol | 2.068 / — / — / 4.165 |
| Bob TPS54331 | BLOCKED before authoring; no library or symbol | 2.083 / — / — / 4.272 |

Both LM358 deliveries have SHA-256
`67766c0cf0d6ce85b9042df94ce4766deb264e917988df1e45d782e9fbaabed2`.
Both buck deliveries have SHA-256
`21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2`, an exact typed-design
association and a qualification report identifying those same delivered bytes. Each buck
qualification is 4 PASS / 12 UNKNOWN with `family_qualified=false`. General simulation time
includes 111.953 s ordinary judgment and 44.520 s qualification; Bob includes 113.779 s ordinary
judgment and 45.164 s qualification. Stage totals include reading, binding and publication as well.

The general plan hash is
`19530176920a72d67c1e9531138c5c20bd1e2d91dc123db4453a023d8c3eb267`; Bob's is
`331fd872052ffb0491b2c792276ad0c01a49196b0d5d629e6d490d8a9df1c71c`.
Both plans froze before generation; their hashes differ because simulator implementation hashes
are edition-specific. The 106-row current buck extraction is distinct from the frozen 19-row
regression: the latter retained 12 PASS / 4 FAIL / 3 UNKNOWN for TPS54332 and 19 PASS for LM358,
with `changed_rows={}`. Those frozen results do not describe the new extraction's coverage.

The following earlier standalone qualification receipts preserve the wrong-candidate control:

| Retained real-LTspice evidence | Observed result |
| --- | --- |
| TPS54332DDA clean candidate, controls enabled | 4 nominal PASS, 0 FAIL, 12 mandatory UNKNOWN; qualification UNKNOWN; 87.497255 s |
| Same frozen plan, candidate VREF changed to 0.72 V | 3 PASS, 1 FAIL, 12 UNKNOWN; VREF measured 0.719933 V against the unchanged 0.772–0.828 V band; 83.232343 s |
| Plan identity across both candidates | `548871ecfa2b5ef595b85294ed3fc0cefbc7366b9d4a78185e64a0ed1dbf8234`; serialized plan unchanged |
| Clean candidate / changed candidate SHA-256 | `d9b0b5e3d4729168933f1a834e49d4e25588dfb6239cf420047c00f999d913e2` / `6ab382f439e8811c8560e7feb8cbdb1a669090338a92fe49b5a030020cf9a7cb` |
| Four synthetic fault controls on the clean candidate | All four rejected; each clean/fault pair discriminates; controls excluded from device counts |

Evidence is local and git-ignored in the general checkout at
`runs/qualification-acceptance/acceptance.json`, with immutable per-execution receipts under its
`clean/` and `wrong/` directories. `run_acceptance.py` is the reproduction entry point and names the
local frozen source files and LTspice executable. These are simulator runs of the shared qualification
code, not a Bob provider run or a final end-to-end published-model acceptance run. The default omits
the optional controls and performs four nominal simulations; an earlier observed run took about
45 s, but its summary receipt was not retained. This is not an end-to-end build-time claim.

TPS54331 now has a partial reviewed extraction profile for the exact TI SLVS839H PDF hash
`cf72dfd0ac69eec645b7b493de628dc1c3aa66f5f925a2ea9be6bb5c38260730`. Its source columns remain
VIN UVLO MAX 3.5 V with no typical value; EN TYP 1.25 V / MAX 1.35 V; current limit MIN 3.5 A /
TYP 5.8 A with no maximum. Row quantity identities prevent unrelated EN/VREF and supply/current-limit
rows from becoming false conflicts. Explicit loaded VREF/current-limit fixtures come from cited raw
rows, with the soft-start guard intact; a VIN operating range is not mistaken for a singleton test
condition. Supplied historical requirements and bindings retain precedence and are never rewritten.

The fresh reviewed-profile run remains BLOCKED: `UVTH`, switching-frequency, soft-start and enable/UVLO
bench coverage, and unresolved D versus DDA packaging remain gaps. The DDA-only PowerPAD is recorded,
and saved common-pin maps retain a package-ambiguity marker that blocks publication even if later
numeric inputs pass. The retained run in `../tps54331-reviewed-takeover/replay-summary.json` took
5.034 s, recorded `provider_calls=0`, complete accounting and `refused_before_authoring`, and wrote no
library or symbol. That receipt predates the final singleton-VIN parser correction.

Publication provenance records the actual delivered library and symbol hashes and pin order.
Resumed repairs invalidate a prior exact design association when library bytes change; absent typed
provenance is marked unavailable. Publication preserves candidate bytes and refuses a harness report
whose model hash or frozen spec digest does not match. Qualification receives that exact delivered
library and its exact typed-design hash when available. A refused rerun archives prior app-owned
deliverables under `build/publication-history/` instead of presenting them as current output.
The focused publication regressions verify these associations and refusal paths, not electrical
accuracy. General API attempts are counted at transport, including retries. Bob Shell
starts are counted separately, with its internal provider-request total left unknown after a start.

The window exposes engine and family choices, and the CLI/window/API default to code-built
`behavioral`. `legacy_ai` remains an
explicit route, with no silent fallback. MCU/FPGA/CPLD/processor/SoC evidence in the part identity or
first-page identity takes precedence over family hints; incidental application mentions do not
become device identity.

Focused checks observed during this change: general pipeline subset 90 passed / 7 deselected; Bob
93 passed / 7 deselected; final fixed-bench plus reviewed-profile subset 27 passed in each edition.
The commands were `python -m pytest -q tests/authoring/test_tps54331_reference.py
tests/pipeline/test_publication_provenance.py tests/pipeline/test_make_model.py
tests/pipeline/test_support_gate.py tests/pipeline/test_publish_guard.py -m "not ltspice and not network"`
and `python -m pytest -q tests/authoring/test_buck_fixed_bindings.py
tests/authoring/test_tps54331_reference.py -m "not ltspice and not network"`, with each work checkout's
`src` on `PYTHONPATH` and the corresponding canonical repo venv. Ruff check/format and diff whitespace
checks passed for these changed files. Final integrated suite and shared-manifest results are recorded
separately when observed.

Final offline command in each edition: `pytest -q -m 'not ltspice and not network'`.
General reported **2203 passed, 20 skipped, 190 deselected in 97.30 s**.
Bob reported **1961 passed, 31 skipped, 187 deselected in 76.09 s**, with four pre-existing
unregistered `slow` marker warnings. Legacy-specific reopen, GUI sweep and network fixture tests
now request their legacy route explicitly. Shared-core checks found 49 intact files per edition, all 49 identical;
repo-wide Ruff check, Ruff format check and diff whitespace checks passed in both editions;
no installer build was performed for this milestone.
These final suites include the citation replay, resumed-design and public BLOCKED regressions.

`tests/pipeline/test_qualification_publication.py` also passed in both editions. It verifies that
the plan freezes before publication and qualification receives the exact delivered library path,
model hash and typed-design hash. This is an integration regression with a stubbed simulator, not
an additional live end-to-end acceptance receipt.

Still open: the twelve mandatory qualification gaps, an honest TPS54331 UVTH policy and resolved
package, M6's package/symbol confirmation gate, and further families and pinouts. No universal
functional coverage, full-family qualification, new installer release or live Bob inference is claimed.

## 2026-09-29 — hand-off takeover: retained failures, honest timing, bounded pin scope (D-058)

The LM5116 pin-only failure now leaves its measured `pin-only-report.json` even though the library,
symbol and model card are withheld. An offline replay of its saved extraction with real LTspice 26
confirmed the report (9.954 s, no provider call). Its required VIN, VCC and HB pins also showed that
the one-rail shell cannot judge additional required supplies: a second offline replay now refuses
that table before rendering or LTspice as `pin_only_required_secondary_supply` (4.544 s, zero calls).
Optional auxiliary supplies remain allowed.

`run-timing.json` now records inference HTTP attempts, including retries, in the general edition.
Bob records the number of shell invocations and marks the internal vendor-call total unknown after
one runs; local and cached routes report exact zero. Timing routes distinguish refusals from agent
authoring. The current-limit matcher reads both spaced and hyphenated wording without accepting
unrelated words.

The TPS54331 hand-off diagnosis needed correction: the saved extraction misplaces MIN/TYP/MAX values,
and `UVTH` is VIN undervoltage lockout, whose typical threshold the datasheet leaves unspecified.
An offline replay after the wording fix remains BLOCKED for cited UVTH and independent VREF and
current-limit tests (6.685 s, zero provider calls). Historical run data and its original PDF were
not edited; no new live AI call was made. A reviewed, exact-document extraction revision and an
honest UVTH corner policy remain prerequisites to supporting this second buck.

| Check | Result |
| --- | --- |
| General offline full suite, rerun alone | 2081 passed, 20 skipped, 190 deselected |
| Bob offline full suite | 1784 passed, 31 skipped, 187 deselected |
| Repo-wide Ruff check and format | passed in both editions (352 general / 310 Bob files formatted) |
| Shared-core manifests and cross-edition comparison | 46 files intact and identical |

The first general full run overlapped Bob's and hit a Windows access-denied error while saving the
GUI sweep's temporary config. The isolated sweep and the serial full rerun passed; no engine test
failed in that run.

Remaining scope: only TPS54332 and LM358 have code-built behavioral implementations. The command
line exposes `--engine` and `--family`, but the window does not; `legacy_ai` remains the default.
More PCB families and independent tests are needed before broad coverage can be claimed.

## 2026-09-29 — engine step 3, second half: pin-only mode, live datasheet runs, automated checks green (D-057)

Pin-only is a real route, the block list is wider, real datasheets have been run through the local
routes with a live extraction, and the automated checks on `main` are green for the first time since
2026-09-26. Support is still not a pass. Two families are built by code, one part each.

| Check | Result |
| --- | --- |
| Live extraction, XD7660 charge pump, `--engine pin_only` (opencode_go, deepseek-v4.1-flash, one key) | UNKNOWN by design in 157.9 s: extract 154.5 s (4 tasks, 0 cache hits, 17 pages, 43 rows, 8 pins), gate 5 benches 2.9 s, other stages under 0.3 s; 12 checks pass, 1 unknown (no short-circuit rating cited), 0 fail; no agent turns |
| Live extraction, LM5116 controller, `--engine pin_only` | BLOCKED, and correctly: the gate failed the current-conservation and quiet-when-clean checks on the 21-pin shell, so nothing was delivered and no other route ran. 347.9 s: extract 342.4 s (8 batches, 240 rows, 21 pins), gate stage 0.07 s, benches 4.2 s |
| TPS54331 second buck, `--engine behavioral`, cached extraction, deterministic bind | refused as `unsupported_family` in 9 s: the buck implementation matches, but the rows do not cite the current limit or the enable threshold and no independent row covers switching frequency, current limit, soft start or enable/UVLO (6 of 112 rows bind deterministically) |
| TPS54331 again with `--plan-tests` (explicit AI planning, cached extraction) | refused again, `unsupported_family`, in 293.7 s (the planning call is 284.2 s of it): 10 of 112 rows now bind, and what is missing fell from four independent tests to two (reference voltage, current limit) plus the two cited inputs (current limit, enable threshold); no model written |
| Pin-only on parts that already have behaviour | LM358 12 pass, 1 unknown, 0 fail; TPS54332 13 pass, 1 unknown, 0 fail |
| LM358 from the datasheet PDF alone, internet switch off | built by the behavioural route in 25.4 s with no provider call; from the reviewed rows 29.0 s |
| Automated checks on the pushed tips (`aed2bb0`) | ruff check, ruff format and the full pytest step all pass on this edition |

The live runs in the table were made with the general edition build; the engine code is the same in this edition (mirrored, shared core identical).

What changed.

- `--engine pin_only`, the open-drain fix in the gate, and the wider block list (D-057). The local routes
  are no longer stopped by the agent network pre-flight, because they never call the agent.
- AI test planning is only on the legacy route. `--plan-tests` asks for it explicitly on a local
  route; the first live attempt on TPS54331 ran past 11 minutes with nothing to show.
- The red automated checks had one cause: `tools/tps54332_m4b1_verify.py` set
  `BOARDMODELER_NO_NETWORK` when imported, and a test imports it, so every test collected in the same
  session saw the network pinned off (49 failures in this edition, 105 in the general edition, plus 8 setup errors
  because the frozen spec is git-ignored). The pin moved into the script entry point, the conftest now
  refuses a run in which an import changes it, the test skips when the frozen spec is absent, and five
  fixtures now declare the family the gate asks for. No product code changed for this.

Not done, stated plainly.

- One part per family. TPS54331 is not a second supported buck: even with explicit planning (5 minutes,
  not the 11-plus of the first try) it lacks the cited current limit and enable threshold and two
  independent tests, so the extraction has to cite them before the claim can be made.
- The pin-only shell has one rail and one ground and clamps outputs to 0 V to the rail, so it cannot
  represent the negative output of a voltage inverter such as XD7660; its card asks for the pin table
  to be confirmed. LM5116 shows a multi-rail controller is withheld by the gate; the failing gate
  report is not saved on a block, so the exact cause is not inspectable yet.
- The op-amp probes fix the eight-pin dual pinout. The window exposes neither `--engine` nor
  `--family`. Provider calls are not counted in the timing record.
- The terminal setup landed on `main` on 2026-09-30 (entry at the top of this file).

## 2026-09-28 — engine step 3: a second family built by code, the pin shell merged, family read by points (D-056)

The pin-shell work (viability gate, pin shell, in-model alarms, datasheet digest) is merged into the
engine branch, and the behavioural route now builds a second, materially different family from cited
rows: a dual op amp, the LM358 (analog signal chain, against the buck converter of step 1). Its design
is typed and read from cited rows only, rendered on the pin shell, and judged by the op-amp probes in
real LTspice; no agent, no provider call, no network. `--engine` and `--family` are on the command
line. Support is still not a pass, and this does not establish universal coverage: two families, one
part each, are built by code.

| Check | Result |
| --- | --- |
| Real build, LM358, `engine="behavioral"`, real LTspice 26, agent backend construction and socket connect made to fail the test | passed in 34.1 s with zero author turns: 32 measured rows PASS, 0 FAIL, 10 rows outside the tested scope with a reason (the general edition split its 31.5 s as read 6.5 s, extract 7.4 s with the two-reader citation check, bind 0.02 s, gate 0.01 s, author 17.5 s all LTspice judging, save 0.06 s). The earlier agent-authored LM358 runs took 216 to 356 s |
| Real build, TPS54332 buck, same route | 139.9 s in the test with other test runs sharing the machine (117.8 s on a quiet machine in the general edition, same code); 12 PASS, 4 FAIL, 41 UNKNOWN, 49 NOT_APPLICABLE as at step 2. The second PDF reading costs 0.5 s |
| Viability gate on the rendered LM358 | no failing check, 12 PASS, 1 UNKNOWN (the short-circuit limit: the rows carry no such number), 17.4 s |
| New tests | `tests/models/test_op_amp.py` 13 (12 offline on a built-in stand-in spec: cited inputs, purity, exact provenance, tamper refusals, withdrawal of the claim, unverified rows, pinout, inconsistent values; and the viability gate in real LTspice), `tests/pipeline/test_behavioral_op_amp.py` 1 (the real build above, local frozen rows and PDF), and more cases in the support, gate, command-line and citation tests |
| Fast suite, Bob | 1550 passed, 49 failed, 20 skipped, 277 deselected; the 49 are the same test ids as the untouched baseline (internet access is off in the local config) |
| Hygiene | `ruff check .` passes; `ruff format --check` clean on every changed file (nine other files were already unformatted on main); shared core 46 files intact and identical across editions |

What changed, in order of importance.

- `models/op_amp.py` (new): `OpAmpDesign`, `design_from_spec`, `render_library`, `OpAmpSeed`,
  registered as `dual_op_amp`. Essential inputs are cited typical values of probe-bound rows; the output
  current limit and resistance are labelled defaults. The claim is withdrawn by removing one typical
  value or one bound row (tests).
- The behavioural route seeds from the implementation the support decision named
  (`Implementation.seed`, `template`, `heading`); the provenance file, model-card heading and timing
  route name follow it. The agent route is unchanged.
- The family is read by points (part number and title, then the head of the first page, then two or
  more distinct row signals) instead of by the first phrase found, after a scan of the frozen specs on
  disk showed a PWM controller labelled passive and a buck labelled supervisor, and the LM358 rows
  unclassified under a file-name title. `--family` names it when the evidence does not.
- Citation verification accepts a page when either of two PDF readers contains the excerpt (D-056).
  Without it every frozen LM358 citation failed to verify and the gate correctly refused the part.

Not done, stated plainly:

- One part per family. A second buck or op amp, and a third family, need extracted rows for a
  datasheet other than the two TI ones on this machine; that extraction is an AI step and was not run.
- `pin_only` still refuses (`pin_only_unavailable`); the open-drain, per-rail and digit-first-name
  fixes to the shell and the judge that the universality battery needed are still open.
- The op-amp probes fix the eight-pin dual pinout, so a single or quad op amp is outside `behavioral`.
- The window does not expose `--engine` or `--family`; provider calls are not counted in the timing
  record; no live provider call was made.

- The LM358 rows for the real build are the general edition frozen ones (git-ignored, copied to
  `models/L1-lm358/spec` here, no PDF); the offline tests use a built-in stand-in spec. One Bob test
  fixture (a zero-coverage spec in `tests/pipeline/test_make_model.py`) now names its family with the
  new option, since nothing in the fixture does and the gate refuses a part nothing identifies.

## 2026-09-28 — engine step 2: the support gate, blocked classes on every route (D-055)

Second step of the engine review the owner named the north star. One decision now sits in
front of every generation route: may the engine claim to support this part? A microcontroller,
FPGA, CPLD, processor or SoC is refused on every route, legacy AI included. A part nothing
identifies is refused, not assumed fine. A part is called supported only when a behavioural
implementation positively matches it, every essential input is cited and every essential
behaviour has an independent bound test; today that is the peak-current buck alone. Support is
not a pass: verdicts still come from the LTspice rows. This does not establish universal
coverage.

| Check | Result |
| --- | --- |
| New tests | 193 passed: 185 in `tests/models/test_support.py` (families, blocked classes, wording, the registry, refusals on every route) and 8 in `tests/pipeline/test_support_gate.py` |
| Real build, frozen TPS54332 spec, `engine="behavioral"`, real LTspice 26, agent backend construction and socket connect both made to fail the test | passed in 122.1 s (the general edition took 117.2 s). Bob: read 2.1 s, extract 2.4 s (supplied spec), bind 0.06 s, gate 0.02 s, author 117.5 s (all LTspice judging), save 0.04 s; zero author turns; delivered library sha256 `21b3c7f1…` (the frozen SW library); 12 PASS, 4 FAIL, 41 UNKNOWN, 49 NOT_APPLICABLE; the four FAIL rows stay |
| Fast suite, Bob | 1485 passed, 49 failed, 19 skipped, 260 deselected in one run; the 49 are the same test ids as the untouched baseline (internet access is off in the local config) |
| Hygiene | `ruff check` and `ruff format --check` clean on every file this step changed; `git diff --check` clean; shared core 44 files intact and identical across editions. The repo-wide `ruff format --check` still lists nine unformatted files that were already on main (`installer/package_portable.py`, `authoring/sanity.py`, `providers/bob.py`, `simulation/ltspice.py`, `storage.py`, `ui/model_maker.py`, `tests/authoring/test_backends.py`, `tests/gui/test_model_maker.py`, `tools/shared_core.py`); `ruff check .` passes |

What the gate does, in order. `read` still refuses a class the part number or the datasheet
title names, before extraction. After `bind`, `models/support.py` decides with the cited
rows as well, writes `support-decision.json` (also for a refusal), and refuses the route
when it is closed: blocked class on all three routes, unclassified on all three, an ordinary
family with no implementation on `behavioral` only. `legacy_ai` stays the default and is
today's agent authoring, unchanged, and it never declares support. `pin_only` is reserved and
refuses with `pin_only_unavailable`. No route falls back to another.

Not done, stated plainly:

- Only one implementation is registered (the peak-current buck). An op amp, an LDO or a
  comparator has no behavioural implementation yet, so on `behavioral` each is refused as
  `unsupported_family`; no breadth across families is demonstrated by this step.
- The vocabulary that identifies a family is keywords, not proof. An ordinary part whose
  number, title and first cited rows name no family is refused as unclassified on every route.
- `--engine` is not exposed on the command line or in the window yet; both still use
  `legacy_ai`.
- Provider calls are not counted in the timing record, and no live provider call was made.
- One Bob test fixture (a zero-coverage spec in tests/pipeline/test_make_model.py) now says comparator hysteresis: the gate refuses a part nothing identifies, so a fixture with no family wording was refused before the stage it exercises. The gate was not changed.

## 2026-09-28 — engine step 1: typed buck design and exact provenance (D-054)

First step of the engine review the owner named the north star, and behaviour-preserving.
The buck template's what-to-build is now a strict, versioned `BuckDesign`; the library is
`render_library(design)`, a pure function of it; a build that used the buck seed saves
`model-design.json` tying the design to the delivered bytes, and every build saves
`run-timing.json`. Routing, equations, scoring, status names, providers and the interface
are unchanged. This does not establish universal coverage and does not change which parts
are supported.

| Check | Result |
| --- | --- |
| Rendered bytes against baseline `b1ced1c` | identical for 8 synthetic specs (SW and AVG) and for the real TPS54332 values; the frozen TPS54332 spec still renders sha256 `21b3c7f1…` |
| New tests, `tests/models/test_buck_design.py` | 33 passed: golden bytes, canonical serialization, 12 rejected-input cases, cited against default, SW/AVG parity, topology rejection, delivered-byte association, stage ledger |
| Buck tests against real LTspice 26 | 57 passed in the general edition (31 s) and 57 in Bob (36 s) |
| Fast suite, Bob | 1293 passed, 49 failed, 19 skipped, 259 deselected in 72 s. The 49 are the same test ids as the untouched baseline (1260 passed there); the difference is the 33 new tests |
| Hygiene | `ruff check` clean, changed files formatted, `git diff --check` clean, credential scan 0 findings, shared core 44 files intact and identical across editions |

Real build on the frozen TPS54332 spec (scripted author, no AI provider, real LTspice):
239.1 s in total. read 2.3 s, extract 2.8 s (supplied), bind 0.06 s, author 233.9 s, save
0.03 s. The author stage held the template's first judging (117.4 s) and one scripted turn
that re-ran the same 19 rows (about 116 s) without improving them, so a route with no repair
turn would spend about half of that. Result 12 PASS, 4 FAIL, 41 UNKNOWN, 49 NOT_APPLICABLE:
the 4 FAIL and the UNKNOWN rows are unchanged and stay open. `model-design.json` reported
association `exact`; 20 parameters are cited or derived from cited bounds and 3 are template
defaults.

Not done: provider calls are not counted in the timing record yet; nothing here matches,
blocks or supports a part class, which is the next step. The Bob edition's `make_model.py`
differs from the general one; the three small edits were ported by anchor.

## 2026-09-28 — pin model, hour 1: viability gate, pin shell, alarms, digest (D-053)

Branch `pin-model` in both editions; the M4b2 work was parked on `wip/m4b2` first and nothing was
pushed. The [evidence report](evidence/2026-09-28-pin-model-h1/REPORT.md) records the LTspice
runs, hashes and limits. Observed results:

| Check | Observed result |
| --- | --- |
| Gate on the committed LM358 (`fixtures/models/lm358_committed_2026-09-24.lib`) | FAIL: 5 checks (node-0 return, 998.8 A residual, 499.4 A short vs 60 mA rating, supply carries 0.7 mA of 998.8 A, inputs 19-21 V above ground pin) |
| LM358 on the pin shell, gate | PASS, 28 benches, 21 s, 7 alarms proven |
| LM358 on the pin shell, frozen 19-case / 32-row spec | 32 PASS, 0 changed, 16.3 s |
| Pin-only MCU8, gate | PASS, 9 benches, 6.6 s, 4 alarm kinds proven |
| Mutants (7 static, 6 dynamic, 5 alarm) | each fails the check that names it |
| Existing buck template, gate (informational) | SW: FAIL `output_short_limited` (PH 28.86 A vs 6.5 A); AVG: FAIL `supply_carries_output_current`, 1 bench UNKNOWN |
| TPS54332 p.6 digest | UVLO 3.5 V = MAX; EN 1.25/1.35 = TYP/MAX; current limit 4.2/6.5 A = MIN/TYP |
| Gate and shell tests, both editions | 41 passed each (LTspice benches included); digest 6 passed each |
| `tools/shared_core.py --compare ..\spice-maker-bob` | identical: 46 files |
| Fast subset, general edition | baseline 1483 passed / 105 failed (machine state: INTERNET ACCESS off in the local config); final 1514 passed / 105 failed, the same 105 tests, no new failure |

Not done: no regulator (buck, LDO) is rebuilt on the shell; `part_class.py` still refuses
microcontrollers and FPGAs; the pin table is hand-written for the proof parts.

## 2026-09-28 — terminal transition: Setup.cmd and a text menu instead of a window

The active product is being changed from a desktop window and compiled installer to a source ZIP with `Setup.cmd`, one `.venv`, and a text menu. This file records current observations. Earlier release history is preserved verbatim in [`docs/evidence/2026-09-28-terminal/history/STATUS-before-terminal.md`](evidence/2026-09-28-terminal/history/STATUS-before-terminal.md).

## Baseline from a fresh clone

| Edition | Command | Observed result |
| --- | --- | --- |
| Bob, `main` cf6b430 | `.venv\Scripts\python.exe -m pytest -q` | 1,368 passed, 55 failed, 156 skipped, 8 errors in 80.53 s without `LTSPICE_EXE`. |
| Bob, `main` cf6b430 | Same command with `LTSPICE_EXE` set to the local LTspice executable | 1,369 passed, 55 failed, 155 skipped, 8 errors in 78.35 s. |
| Bob, `main` cf6b430 | `.venv\Scripts\ruff.exe check .` | Passed. |

These are pre-refactor results. The fresh clone lacks the ignored `models/T1-tps54332/spec/requirements.json`, which accounts for the eight errors. Many Bob backend tests assume configured Internet access; a fresh copy has no setup config and refuses network calls before reaching their mocked provider path. Tests that require a configured LTspice executable also fail or skip in a fresh copy. These failures must not be reported as regressions from the terminal change.

## Implementation observations

- The Bob runtime lock now exports eight pinned packages with SHA-256 hashes. `uv.lock` lists Windows x64 and ARM64 wheels for numpy, pydantic-core, and pypdfium2.
- On Windows 11 x64, a committed source ZIP extracted under a path with spaces, `é`, and `Ω` installed the eight wheels, passed a real LTspice smoke test and `doctor`, and created, refreshed, and removed a Unicode-safe shortcut.
- The focused suite passed 51 tests; Ruff lint and format checks passed. The broader baseline suite had missing ignored test data and setup-dependent failures and is not described as green.
- Stubbed no-Python, installer-hash, and unsupported-architecture tests passed. Both Python 3.14.7 installers were downloaded and hash/signature verified; their real per-user install steps passed on fresh GitHub Actions x64 and ARM64 runners ([run 36501631540](https://github.com/BasamAhmed640/spice-maker-bob/actions/runs/36501631540)). The installers were not run on the developer PC; full source ZIP setup has not been run on ARM64 or Windows 10.

Exact commands and untested cases are in [`docs/evidence/2026-09-28-terminal/REPORT.md`](evidence/2026-09-28-terminal/REPORT.md). No live Bob authoring call has been made for this change.
