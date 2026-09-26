# Engineering decisions — Spice Maker Bob

## Bob-only application

IBM Bob is the only AI shipped in this edition's source, UI and documentation. The
catalog holds exactly one entry and the author factory constructs Bob Shell or refuses
the configuration. A saved incompatible selection is not silently replaced or displayed
by name. Only an explicit USE IBM BOB selection followed by SAVE changes it.
Bob owns its model and thinking policy; the adapter does not fabricate an unsupported knob.

## Source evidence and limits

The exact requested part is included in extraction and cache identity. Family-shared
rows remain applicable, but limits exclusive to a different suffix cannot be substituted.
Citations must match the stored document text; typical values and absolute maxima do not
become guaranteed operating limits. Remote permission may change for an otherwise
identical document; other metadata conflicts remain rejected.

## Frozen specification and observed verdicts

The specification is frozen and rechecked through the authoring loop. Bob may repair
model files, not alter evidence, weaken limits or remove tests. A PASS requires actual
simulator output at recorded conditions. Missing measurements, tampered evidence and
unsupported rows remain explicit gaps. Nominal behavior does not qualify temperature,
statistics, high-speed protocol or op-amp behavior that has no appropriate probe.

## Credentials and diagnostics

Credentials live in the OS keyring or the documented process environment. No secret is
written into project files or commands. Diagnostic responses are redacted before any
excerpt slicing so boundary fragments cannot escape exact-match redaction.

## Caching and repeatability

Extraction identity includes target part, schema/context, source content and backend.
A verified candidate may skip repeated authoring. Simulator receipts are process-local;
a fresh process re-judges the model before claiming current evidence. Model/spec/tool
changes or missing/changed artifacts invalidate reuse.

## Packaged delivery

The main repository includes Install.exe because the user's required acquisition path
is Code → Download ZIP. It remains the real animated installer, not a download pointer.
Builds isolate the runtime search path and must launch the actual frozen Qt window before
packaging. The prior startup failure was a mismatch between required unversioned ICU
symbols and the bundled library's versioned exports; the library's original provenance
was not established. No complete binary reproducibility or signing claim is made.

## Shared engineering without overwriting Bob policy

Only reviewed, provider-neutral files are synchronized between editions. The sync tool
must preserve edition-owned catalog, adapter, UI, branding, tests and documentation.
Source hashes and observed verification results identify a release; a passing old build
is not proof of a newer binary.


## Elapsed build time (1.1.4)

The GO timer uses a monotonic clock and a GUI event timer, independent of worker
progress signals. It starts for an accepted build, includes cancellation cleanup,
freezes on the terminal result/error, and resets on the next GO. It is not an ETA.
The agent workflow is documented in AGENT_WORKFLOW.md without claiming unmeasured
model accuracy or exposing private reasoning.


## LM358 qualification (1.1.5)

Use a hash-bound reviewed extraction profile for the exact supplied LM358 datasheet, preserving explicit coverage gaps and rechecking citations. Qualify both the instruments and the generated model in LTspice; typical comparisons prevent idealized zero-offset/bias candidates passing maximum limits alone. Add native complex AC decoding and use it consistently when revalidating cached reports. See LM358_VALIDATION.md for scope.


## 2026-09-20 — application-owned symbol layout

Every authored model is published with the deterministic rectangle renderer, including
cache hits and command-line publication. Agent drawings are not used for the final
symbol. Datasheet pin directions guide placement; physical package pin numbers never
replace `.subckt` positions. Validation checks each pin's actual order, as well as the
set and bijection. The agent is asked to write only the electrical model, saving the
symbol output tokens. See STANDARD_SYMBOLS.md.


## 2026-09-20 — preserve compound quantity dimensions

Datasheet extraction and frozen characteristics share the validated unit parser.
Prefixes on quotient operands are independent: `ns/V` is `1e-9 s/V`, whereas
`V/ns` is `1e9 V/s`. No reciprocal conversion is implied. Thermal resistance in
Celsius per watt and kelvin per watt uses temperature differences, not absolute
temperature offsets. Unknown units and incompatible dimensions remain rejected.


## 2026-09-20 — independent fixtures and explicit coverage

Use bounded declarative test recipes for diverse physical pin maps; freeze recipes and limits before authoring. Preserve untested quantitative rows as UNKNOWN. Unknown units are retained without conversion and cannot be measured; stress ratings have an explicit class. Cache successful extraction batches; keep observed UNKNOWN feedback in memory for repair, never as a shortcut to PASS. A failed API turn with unchanged bytes stops. Local unambiguous syntax/ground corrections retain original bytes and require a new simulator run. Correct fixture thresholds only from explicit source conditions before freezing a new run.

## D-020 — bounded credential verification

Saving a key retains it in the OS credential store even when a short connection
check is inconclusive. Only an observed inference response earns VERIFIED. Timeouts,
quota, setup and incomplete replies remain UNVERIFIED. HTTP 401/403 are reported as
authentication/permission refusal, without echoing server text. Checks send no user
documents and cannot change the provider, model or reasoning choice. Bob license
acceptance is explicit and is never performed automatically by the application.


## 2026-09-21 — the credential file is plain local JSON, not DPAPI ciphertext

Supersedes the 2026-09-20 entry below. At the owner's requirement that no Windows
keys be saved anywhere, DPAPI is no longer used: the key is an ordinary local JSON
file, `data/credentials.bob.json` in this edition's extracted folder (`credentials.json`
for the main edition). Nothing is bound to Windows — no ciphertext, registry entry,
Credential Manager entry or user-profile location — and no credential is written outside
the extracted copy. The tradeoff is accepted and stated in `AGENTS.md` §7 and
`INSTALL.txt`: the file is not encrypted, so anyone who can read the folder can read the
key. Saving a provider replaces the prior saved key; explicit environment variables still
support CLI automation.


## 2026-09-20 — one encrypted local credential per edition

At the user's request, Windows Credential Manager is no longer read or written.
Current-user DPAPI protects a single local credential file in SpiceMakerData or
SpiceMakerBobData under LocalAppData, outside the installer-managed folders.
Saving a provider replaces the prior saved credential. No plaintext fallback,
machine-wide protection, hard-coded encryption key or automatic credential export
is provided. Explicit environment variables still support CLI automation.
Defaults are omitted from settings JSON; model evidence and caches remain separate.
Existing vault entries require explicit user-authorized migration/removal; the app
does not enumerate or delete a user's other saved credentials.


## 2026-09-20 — scope extraction to the selected datasheet

A model run must pass its registered document ID to extraction. Other documents
left in a shared output folder are excluded before text collection, egress checks,
disclosures and cache keys. The board-project API keeps its explicit all-document
default. Excluding unrelated files must not change their stored originals.
Installer builds must match the application and project version. Windows packaging
can run on GitHub's hosted runner; this does not change local Windows security policy.

## 2026-09-20 - Portable folder is the storage boundary

The user requires installation and all application-owned state under the extracted
GitHub folder. Do not restore global config or keys. First launch requires explicit
setup. The installer creates no global install/shortcut/credential entries. Updates
replace app/ only. Scratch directories and model exports stay local; the credential file is
plain local JSON in `data/`, not DPAPI ciphertext (see the 2026-09-21 entry above).
Normalize only unambiguous flat citation page numbers; conflicting page numbers remain
validation errors. Preserve all numeric requirements and reasoning settings.

## 2026-09-20 - Respect stream completion and expose real network activity

HTTP SSE [DONE] ends the response without waiting for connection EOF. The decoder
still rejects missing finish reasons. Progress is per request, carries counts only,
and cannot mix concurrent batches. Metadata section routing leaves full electrical
requirements intact and falls back to all pages when pin headings are unrecognized.


## 2026-09-21: user-selected quick default

The user requested local sanity checks instead of waiting for full simulation verification. GUI quick mode skips independent AI test planning and the full electrical simulation suite, preserves every extracted row, and labels exported models electrically unverified. Full verification remains available explicitly. No numerical tolerance or measured PASS requirement is weakened. Source/CLI full defaults remain backward compatible; the CLI offers --sanity.


## 2026-09-21: bounded load checks and local source repair

Quick mode performs a generic unpowered LTspice load under a five-second deadline
when the simulator is available. Explicit syntax rejection withholds the export;
timeout or unavailable simulation is displayed as inconclusive/unavailable, never
electrical PASS. Full electrical verification remains optional.
Repairing a behavioral source must update current references only in that source's
own subcircuit, preserving unrelated circuits and original evidence. The sanity
receipt version changes so candidates from the earlier repair logic are not reused.

## D-034 — Convergence before accuracy; repair names lines, not whole models (2026-09-24)

The TPS54332DDA build of 2026-09-24 spent two author turns (868 s, 182k tokens) on
models LTspice could not use: turn 1 read the internal node `en_ok` as a bare name
(LTspice: "No such parameter defined"), turn 2 held its PWM latch on a node whose only
path to ground was 1 TΩ and whose driving source read its own output ("trouble with
node en" after every operating-point method failed). The feedback the author received
named the failing probes, never the lines that caused them.

`authoring/convergence.py` now (1) lints every candidate for bare node names in
expressions, undefined identifiers, self-reading behavioural sources, nodes without a
≤1 GΩ DC path, and datasheet-floatable pins (for example EN, "float to enable") that
the model does not bias itself; (2) reads the LTspice log of the model that just ran
and quotes the rejected or non-convergent lines. The loop appends both as a targeted
repair section. The one repair made without the author is syntactic — a bare node
name inside a B-source expression becomes `V(node,GND)`, with the original bytes
archived under `evidence/bare-node-references/` — because it cannot change what the
model means. Nothing here edits limits, fixtures or verdicts: the harness is still the
only source of PASS or FAIL, and a floating EN in a fixture is treated as valid input.

The author prompt is bounded: testable rows in full (fixture JSON without the
planner's prose), at most 20 not-testable rows as one line each, pin prose clipped.
The same spec produced an 82 KB prompt before and 41 KB after. Reasoning effort is
chosen per stage where the provider documents the switch: `low` for datasheet
transcription, `high` for test planning and model authoring (it was `max` for all).

## D-035 — One shared core, enforced by a manifest (2026-09-24)

`shared_core.json` lists the 41 modules under `authoring/`, `documents/`,
`requirements/`, `simulation/` and `verification/` that must be byte-identical in
Spice Maker and Spice Maker Bob; `tests/test_shared_core.py` fails on drift and
`tools/shared_core.py --compare <sibling>` checks both checkouts. Provider wiring, the
author loop glue, OCR and simulator launch stay edition-specific. Bob keeps its
`.bob/` rules, `.bobignore` and tool-free Bob Shell; nothing in the core gives an
agent a shell or file tools.

## D-036 — Extraction reads the pages that carry specifications (2026-09-24)

`documents/relevance.py` scores each page (specification/pin/thermal headings,
number-with-unit density; mechanical, packaging, revision and notice pages score
negative) and extraction sends only the selected pages. Every page is accounted for in
`evidence/page-selection.json`: selected with its signals, skipped with a reason, or a
named gap. A page with no text layer goes to OCR when an engine is available and is
otherwise an explicit `extract_page_gap` — never silently dropped. A provider reply cut
off mid-JSON is refused as `extraction_response_truncated` before it can be cached.

## D-037 — A second PDF reader, not a stopped build (2026-09-24)

pypdf 6.19.0 raised `NameError: name '_LENGTH_LIMIT' is not defined` inside
`NumberObject.read_from_stream` while registering a readable datasheet (the LM358 PDF,
1 of 7 runs with a provider key in the environment, 0 of 7 without; the error names a class
attribute that exists, so it is not a property of the file). That one fault stopped the
whole build at "read". `documents/pdf.read_pdf` now reads the same inventory — page text,
raster image counts, `/Info` metadata — through pdfium when pypdf raises anything, and
raises pypdf's own error only when pdfium cannot read the file either. Page labels stay
undeclared (`{}`) on the pdfium path, because a label is reported only when the document's
own tree was read.

## D-038 — One gate fixture, then the rest in parallel (2026-09-24)

The harness runs the first fixture alone. A candidate that times out or does not converge
there is still sent back for repair with every other fixture deferred (UNKNOWN), as before.
Once the gate simulates, the remaining fixtures run concurrently
(`BOARDMODELER_HARNESS_WORKERS`, default `min(4, cores)`, `1` restores serial runs), and a
later timeout or convergence failure marks only its own rows UNKNOWN. Before, one 120 s
timeout deferred every remaining fixture — 8 TPS54332DDA rows in one recorded turn.

## D-039 — Readiness is visible before GO (2026-09-24)

The build window shows six lights — API KEY, MODEL (Bob edition: BOB SHELL), LTSPICE, PDF,
OCR, INTERNET — and a VERIFY KEY & TOOLS button. The lights open from local state only
(nothing is sent). VERIFY sends one request in exactly the shape a build sends (endpoint,
model id, reasoning switch, the app's User-Agent) and passes MODEL only when the reply parses
as the generator's `{"files": ...}` object; it then runs the LTspice RC smoke circuit. In the
Bob edition it first reads `bob run --help` offline and turns BOB SHELL red when a flag every
build passes is missing, then runs Bob Shell once with every tool group disabled. No key,
request or response text is ever put in a light's detail. Found while building it: without
the app's User-Agent the OpenCode endpoint answered HTTP 403, which reads as a rejected key.

## D-040 — Symbols follow the schematic convention (2026-09-24)

Generated `.asy` symbols place positive supplies on top, grounds, negative supplies and
exposed pads at the bottom, inputs and controls on the left, and outputs plus the
feedback/compensation network on the right. Numbered channels are grouped: an op-amp channel
reads IN+, OUT, IN− with the output between its own inputs. The old two-column heuristic
matched the hint "a" inside any name, which put POWERPAD among the inputs, VIN at the
bottom-left and VEE among the outputs. Geometry only: `SpiceOrder` still follows the
`.subckt` declaration, checked by `validate_symbol` and by a real LTspice netlist test.
`tools/render_symbol.py` draws an `.asy` the way LTspice places pin names, for review.

## D-041 — Seed recognized buck converters before author repair (2026-09-25)

A cited, buck-specific requirements set and the BOOT/VIN/EN/SS/VSENSE/COMP/GND/PH
pin family can select a deterministic peak-current buck template. The seed records
which values come from the datasheet and which remain template defaults, then goes
through the same LTspice product harness as any authored model. Seeding needs no
provider request. Any later author repair is bounded by measured feedback; an
unverified or partly failing seed remains UNKNOWN rather than being promoted to
PASS. The Bob edition retains its tool-free Bob Shell boundary and has no fallback
to a general-edition provider.

## D-042 — Sanity-check planned circuits before freezing (2026-09-25)

The planner checks a proposed buck fixture's external catch-diode direction,
output capacitor, compensation path and soft-start timing against the cited
device values before the fixture is frozen. A COMP shunt that cannot reach the
control threshold with the cited error-amplifier current, or a steady-state
window that ends before soft start, is refused with a concrete reason. This
does not change the limits or retroactively rewrite already frozen fixtures.
Current magnitudes are compared by absolute value when the cited characteristic
does not specify polarity; the signed simulator measurement is retained, and
explicitly cited direction or negative bounds still use signed comparison.
Switching frequency is measured from consecutive edges, including legacy plans
that use the same signal for trigger and measurement.

## D-043 — Keep template evidence scoped to measured rows (2026-09-25)

The TPS54332DDA seed measured 12 PASS, 4 FAIL and 3 UNKNOWN against 19 frozen
rows; the offline product builds in both editions ended UNKNOWN with the same
counts. A separate official TPS54331 datasheet produced a cited three-row
SpecSet that measured 3 PASS, 0 FAIL and 0 UNKNOWN in LTspice. The TPS54331
datasheet's 3.5 A current-limit figure is a minimum and 5.8 A is typical, so
the template's current-limit default was not presented as a cited maximum or
included in those three measured rows. These results demonstrate the bounded
seed path, not a verified full-device model or a completed release gate.

## D-044 — Treat unreadable Internet settings as off (2026-09-25)

Both editions now refuse Bob and supporting-material requests when the single
Internet setting cannot be read. A damaged settings file cannot turn a
previously saved off choice into permission to send a request. The refusal
names the SETUP switch, environment override and invalid settings as possible
causes. Focused tests cover the unreadable-config path and Bob's refusal
before its process starts.

## D-045 — Logic gates reference their own ground; a bench must touch node 0 (2026-09-25)

LTspice ignores an unused A-device input only when it sits on that gate's own common
(8th) node; on any other node it counts as logic low. The buck template tied unused inputs
to global `0` while each gate's common was the model's `GND` pin, and the fixture loader
had renamed a bench's only ground to `bm_fixture_ground`, which nothing tied to node 0. The
saved current-limit bench therefore never switched (1.21198e-9 A against 4.2 A). Now: the
template ties unused inputs to its `GND`; lint `a_device_input_ground` flags any model that
does otherwise; `GND` is renamed only when the bench also names node 0 (a real ground-
current sense); a bench with no connection to node 0 is refused as `fixture_floating_ground`.
Proof: `docs/evidence/2026-09-25-buck-slice/ground-proof/`.

## D-046 — Buck rules see through sense elements and pin aliases (2026-09-25)

The pre-freeze buck rules found the power stage only through an inductor on a pin literally
named PH. A 0.02 Ω PH-to-inductor sense resistor, or pins named SW/FB/AGND, made every rule
— soft-start timing, COMP shunt, catch diode — silently not apply. PH is now traced through
current-sense elements (≤ 1 Ω, 0 V sources) and terminal names are mapped through the
template's aliases first. Limits are unchanged; the saved 0.5 ms current-limit window is
rejected because cited SS charging needs 3.86 ms.

## D-047 — Template parameters come from what a row says, not its id (2026-09-25)

A fresh extraction names rows generically (`B002_REQ_016`), so the suffix-only mapping gave
the saved GUI build 23 template defaults and 0 cited values — a second buck would silently
have carried TPS54332 numbers. Each contract parameter now also has statement + unit
sources (suffix sources stay first for older frozen specs), a cited typical current limit
is preferred to a bounds midpoint, and the contract states pin-role aliases, ground-tie
pins and supported/unsupported behaviours. `authoring/buck_fixtures.py` builds the
current-limit bench deterministically from the matched pins and cited rows; it must pass
the same pre-freeze rules as an AI-planned bench. It is proven on TPS54332DDA and TPS54331
but not yet used by `bind()` to skip planning.
## D-048 — Model the card-level behavior the user needs to check (2026-09-25)

Spice Maker's target is a system-level LTspice sanity check for an I/O card, not an attempt
to reproduce every datasheet row. A useful model must expose wrong wiring, pin-rule
violations and implausible board behavior. The previous AI-written, row-by-row path has
not delivered a verified functional model; its results remain historical evidence and
are not promoted by this decision. The board-level power-up and fault checks return as
part of the system-model acceptance path, including an untied AGND/DGND pair and an
oscillator overloaded by five clock inputs.

For the exact package, every physical pin number and name must appear, and the `.subckt`
port order must match the symbol's `SpiceOrder`. A pinout may be published only after the
user confirms it or two independent sources agree. The explicit pinout-confirmation gate
also applies before a model receives `system-verified` status. Internal pin-to-pin ties
are forbidden unless a cited datasheet page states that the device makes that connection;
an external PCB connection must stay visible as a required-connection rule. In particular,
the TPS54332 POWERPAD must not be silently tied to GND inside its model.
A high-value leakage resistor may aid numerical convergence, but it cannot act as
a functional pin tie or make a missing PCB connection pass its required-connection check.

The must-be-right measurements are VREF; UVLO rise and fall; EN thresholds; soft-start
time; PG thresholds and delay; switching frequency; quiescent and shutdown current;
current limit at both minimum and maximum corners; and current-sense gain in A/V. Gain
is a measured slope over at least two COMP points above the pulse-skip threshold.
These values must meet cited datasheet minimum/maximum bounds, or ±10% when the only
cited value is typical. A failed or unmeasured requirement cannot become PASS by changing
its limit or test circuit.

Output ripple, switch-node and digital-output edges, load-step dip and recovery, and
startup shape are ballpark checks. Measured magnitudes and times must be within 0.5–2×
of the best available reference: first a vendor model, then a datasheet typical-application
figure, then a textbook estimate using the actual test-circuit parts. Edges must not be
ideal, switching ripple must not be zero, ringing must decay, and startup must rise
steadily unless the reference overshoots. A reference or signal that cannot be measured
leaves that check UNKNOWN rather than granting a pass.

Switching-regulator templates must provide `SW` and `AVG` modes with identical pins and
parameters. `SW` supplies ripple, edge and transient checks; `AVG` supports long
power-up and fault sweeps. Must-be-right checks run in both modes. Ripple and edge
checks on `AVG` return UNKNOWN, never a silent zero or an inferred PASS.

The planned generic path must give every IC a pin model from `PinDefinition` records
and existing primitives,
even without a family template. Its alarms cover absolute maximum ratings, required
connections, floating inputs, power through I/O while the supply is off, and overloaded
outputs. Each alarm needs a fault-injection test that fires and a clean-circuit test
that stays quiet. A generic pin model does not imply verified internal functional
behavior.

In the planned default path, code will build the model from templates or pin primitives
and build its fixed test checklist. AI may read and extract datasheet evidence; it will
not plan tests, write SPICE or repair candidates in that path. The earlier AI-authored
path must remain available behind an explicit flag. `system-verified` will be a stricter
status reached only after the pinout gate and real LTspice measurements support the
applicable claims; otherwise preserve FAIL, UNKNOWN and their reasons. Per-model time
is measured against a 5–10 minute goal without weakening these gates.

## D-049 — Verify fixed buck benches from raw cited rows (2026-09-25)

The M2 bench builder accepts the frozen characteristics only alongside the
matching raw requirement record. It checks `citation_verified`, document ID,
page, excerpt, and SI-normalized value before selecting the VREF, frequency,
gain, pulse-skip, current-limit, or SS-charge row. A page number in a derived
spec alone cannot establish a verified citation. Gain fits use distinct active
COMP points; a scalar current-sense-gain probe is an explicit coverage gap.
Current-limit benches use a resistive overload after calculated SS charging and
settling. Because the TPS54332 SS current is typical only, that calculation is
not a guaranteed latest silicon start time. The two current-limit corners are
subcircuit instance-parameter checks, not claims about silicon process corners.

Generated decks alone confer no electrical verdict. The evaluator records
measured metrics or UNKNOWN, preserving the M1 ripple failure and unresolved PH
edge. The LTspice executable is passed explicitly and the code-built runner
keeps artifacts inside ignored `runs/`; raw waveform files are hashed before
local removal. Evidence: `docs/evidence/2026-09-25-system-models-m2/REPORT.md`
in the general edition.
