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

## D-050 — Keep required PCB ties external and cite each part (2026-09-25)

A template may expose an additional ground or pad terminal, but it may not
silently satisfy that terminal's board connection with an internal low-ohm
element. The TPS54332 PowerPAD remains distinct from GND; its 1 GΩ internal
resistor exists only for numerical convergence. A card-level static check uses
the verified raw `B001_PIN_POWERPAD` citation, physical pins 9 and 7, and the
declared/built netlist to require the actual PCB tie. The generic buck contract
does not reuse the TPS-specific citation for another part.

An internal `chk_powerpad` node supports a synthetic clean/open diagnostic.
The 0.1 V threshold and external 1 nA test injection are test equipment, not
TI limits; the ordinary model injects no diagnostic current. Both clean and
fault runs must yield LTspice raw data before an alarm is called measured.
This slice does not complete the full Card A `GND-02` suite case. Frozen
TPS54332 regressions check input hashes and spec digest as well as row IDs, so
changed limits cannot appear to be an unchanged result. Evidence in the
general edition: `docs/evidence/2026-09-25-system-models-m3/REPORT.md`.

## D-051 — Keep the averaged buck an explicitly measured candidate (2026-09-26)

The default peak-current buck renderer remains the frozen switching model.
`AVG` is an explicit alternate renderer with the same external pins and cited
device parameters. The external inductor value used to estimate average
current is an instance-overridable application-bench parameter, not a
datasheet property of the IC. A synthetic settling or controlled-current
smoke check can reject an unstable candidate, but it does not establish a
datasheet PASS or full M4 acceptance. AVG has no electrical switching ripple
or PH edges to judge; those checks remain UNKNOWN. Input-power consistency,
all cited both-mode limits, SW waveform shape, and Card A fault behavior need
separate evidence before the release checkpoint. Evidence:
`docs/evidence/2026-09-26-system-models-m4a/REPORT.md` in the general edition.

## D-052 — Scope: SPICE models only; no board checker or findings report (2026-09-26)

Spice Maker's product output is an LTspice `.lib`, `.asy` symbol, model card,
and tests that establish what the model can and cannot represent. There is no
board checker, board/CAD/netlist import feature, board findings report in HTML,
Markdown or JSON, or board-report screen or window. Do not expose
`check_circuit` or `reporting/html.py` for board use. Existing board-layer code
stays dormant; internal tests may reuse its helpers, but this decision does
not authorize extending it into a product feature. This supersedes the
board-facing parts of D-048, D-050, D-051 and older plans; their measured
model evidence remains historical evidence.

Model alarms are generated into the model and fire in the user's own LTspice
simulation. Each model card names the alarm trace to plot and its datasheet
page. The 66-case `SYSTEM_TEST_SUITE.md` catalog is a developer test catalog:
each eligible alarm uses a small code-built circuit with a quiet clean control
and a firing fault control. PWR, SEQ, VAL, and PG-to-EN chains may need two or
more modelled parts in those test circuits. GND-03, PWR-04, CLK-03, DIG-05,
BUS-02, and PIN-04 are `NOT COVERED` because only a board-wide wiring check
could catch them. PIN-01, PIN-02, and PIN-05 belong to the model-creation
pinout gate in M6. `UNKNOWN` never counts as detection.

M8 reference-card board runs are `REMOVED`. M4b1, M4b2, M4b3, M4c, M5, M6,
M7, and M9+ continue as model-generation and model-verification milestones in
both editions. The citation, honest verdict, explicit LTspice path, offline,
credential, and self-contained environment rules remain in force.

## D-053 — Judge a model by how it sits in a circuit; build every pin the same way (2026-09-28)

Passing datasheet rows is not viability. The committed LM358 passes 32 rows and still
returns its output current through ground instead of the supply pins, has no output current
limit (a 10 mOhm short carries about 500 A against a 60 mA datasheet maximum), and lets a
floating input rise 19-21 V above the ground pin on a 5 V supply. The fixes are three
mechanisms, none of them per-family:

* **The viability gate** (`authoring/viability.py`) judges any model from its ports and a
  pin table alone. Static: ports match the pin table, symbol `SpiceOrder` matches the port
  position, no use of the simulator's node 0/`GND`, no internal pin-to-pin tie without a
  cited datasheet statement, no structural DC-path error. Dynamic (LTspice, the ground pin
  held 2.5 V above node 0): every pin state converges, the currents into the pins sum to
  zero, outputs are current limited against the cited short-circuit rating, the supply and
  ground pins carry the output current, floating inputs stay inside the rails, supply pins
  draw a quiescent current, NC pins stay inert. A report whose dynamic benches did not run
  is never PASS. A model is graded on its function only after it passes the gate.
* **The pin shell** (`models/pin_shell.py`) renders every package pin as a port, by kind,
  from a confirmed pin table; a function, where the part has one, is a short plain-SPICE
  core inside it. It is one mechanism for any IC, including parts with no function
  (a microcontroller: pins only). An output no core drives is inert until the instance
  parameter `LEVEL_<pin>` commands it, so the gate and the user can exercise it. The buck
  template is frozen: it is not extended, and M4b2/M4b3/M4c are parked on `wip/m4b2`.
* **Alarms are part of the model** (`chk_abs_*`, `chk_ovl_*`, `chk_flt_*`, `chk_tie_*`,
  `chk_any`; plot `V(x1:<node>)` in the user's own run). Absolute maximum, absolute or
  relative to the supply pin (which also covers a chip back-powered through an I/O); output
  overload (demand beyond the cited current limit); undefined digital level (a floating CMOS
  input drifts to mid-rail, the worst case); a required connection missing (an open exposed
  pad, seen through a 1 nA probe current). This supersedes D-050's "the ordinary model
  injects no diagnostic current" for pins the pin table marks `required`: D-052 puts the
  alarm in the model, and an open pin is only visible if something probes it. Each claim on
  a pin table is proven by the gate: present in the model, quiet in clean use, firing on its
  own fault bench.

Tables reach the AI through the **datasheet digest** (`documents/digest.py`): MIN/TYP/MAX
columns are measured from character positions, never guessed from the text layer.

Limits, stated once: the gate and shell are proven on an op amp (LM358, with the frozen
32-row spec unchanged) and a pin-only microcontroller-class part; no buck, LDO or
family with a switching stage has been rebuilt on the shell yet. Nothing here relaxes a
datasheet limit or turns UNKNOWN into PASS. Evidence:
`docs/evidence/2026-09-28-pin-model-h1/REPORT.md`.

## D-054 — Typed model design, exact delivered-byte provenance and stage timing (2026-09-28)

The owner named the outside engine review "Spice Maker — the model engine refactor"
(an evidence-to-model compiler) as the north star for the background engine. This is its
first step and it changes no behaviour. The owner's scope statement of the same day is
recorded for the steps that follow: broad functional coverage of ordinary PCB components
through one reusable engine and shared electrical building blocks; the universal pin model
is a foundation and never a substitute for a component's essential behaviour; MCUs, FPGAs,
CPLDs, processors, SoCs and any device whose required behaviour the engine cannot
represent are blocked on every generation route, legacy AI included; an unknown
classification never defaults to supported; a part is supported only with a positively
matched behavioural implementation, adequate cited inputs and independent functional
tests; pin-only output is a separately requested, clearly limited mode; every FAIL and
UNKNOWN is preserved. This step establishes none of that coverage.

Decision:

1. What to build for a recognised peak-current buck is a strict, versioned `BuckDesign`
   (`models/buck_switching.py`): contract id and hash, renderer version, spec digest, part,
   subcircuit, physical ports, mode and every parameter with its origin (`cited_row`,
   `derived_from_bounds` or `template_default`). A cited value must carry its row, page and
   excerpt; a default must carry none. It holds no bench limit and no verdict.
2. `render_library(design)` is a pure function of the design and `RENDERER_VERSION`; a
   design from another renderer or contract version is refused, not rendered.
   `seed_from_spec` stays as a compatibility adapter and its public payload is unchanged.
3. Each build that used the buck seed also writes `model-design.json` beside
   `template-parameters.json`: the canonical design, its digest, and the hashes of the
   rendered and the delivered library. The association is `exact` only when the delivered
   bytes are the design's own rendering; after a legacy repair it is `invalid_after_change`
   and the design is kept as seed provenance. Final parameters are never reconstructed from
   SPICE text, and free-form libraries get no invented structured record.
4. Every build writes `run-timing.json`: seconds for read, extract, bind, author and save,
   the seed's own judging time, author turns and total. `author` includes the agent turns
   and the LTspice checks inside them; provider calls are not counted yet.

Evidence: the rendered library is byte-identical to the baseline `b1ced1c` for eight
synthetic specs (SW and AVG) and for the real TPS54332 values, and a real build on the
frozen TPS54332 spec delivered library sha256 `21b3c7f1…`, the frozen SW library, with
12 PASS, 4 FAIL, 41 UNKNOWN and 49 NOT_APPLICABLE unchanged in kind. Routing, equations,
scoring, status names, providers and the interface are untouched.

## D-055 — Support gate: blocked classes on every route, unknown never supported (2026-09-28)

New-run defaults and UI route selection below are superseded by D-059; the support safety rules
remain in force.

Implements the owner's scope statement recorded in D-054.

1. `models/support.py::decide_support` is the one place that decides what may be claimed for a
   part. Its states are `blocked_class`, `unclassified`, `unsupported_family` and `supported`,
   and it says which of three routes may run: `behavioral`, `pin_only` and `legacy_ai`.
2. Microcontrollers, FPGAs, CPLDs, processors and SoCs are blocked on every route, legacy AI
   and pin-only included. `authoring/part_class.py` gained 15 written families and text rules
   for system-on-chip, system-on-module, application, media, network, graphics and digital
   signal processors, and single-board computers. The catalog no longer calls this class
   in scope.
3. A part is `supported` only when a registered behavioural implementation positively matches
   it, every essential input of that implementation is cited rather than defaulted, and every
   essential behaviour has an independent bound test. Registered so far: the peak-current
   buck. Support is not a pass: the four failing TPS54332 rows stay FAIL.
4. A part nothing identifies is `unclassified` and is refused on every route. A part read as
   an ordinary family with no implementation is `unsupported_family`: closed to `behavioral`,
   open only to the explicit limited routes. The family is read from the part number, the
   datasheet title and the first cited rows, because metadata titles are often "untitled".
5. `MakeModelRequest.engine` chooses the route. `legacy_ai` is the default and is today's
   agent authoring, unchanged, and it never declares support. `behavioral` builds the code
   candidate and judges it in LTspice with no backend and no agent turn, for supported parts
   only. `pin_only` is reserved and refuses with `pin_only_unavailable` until the pin builder
   and a confirmed pin table exist. No engine falls back to another.
6. Every decision is saved as `support-decision.json`, including refusals.

Limits: the vocabulary is keywords, not proof, so unclassified refusals will be common until
it and the pin-signature rules grow; that is intended. The window does not expose
`--engine` or `--family` yet and still uses `legacy_ai` (the command line does; see item 8).

Revision the same day, after the frozen LM358 rows (file-name title, no family word in the
rows) read as unclassified and a scan of the frozen specs on disk labelled a PWM controller
"passive" and a buck "supervisor":

7. The family is read by points, not by the first phrase in a list. The part number and the
   datasheet title score 6 per phrase, the head of the first page 2 per phrase (at most 4), and
   the cited rows 1 per family signal (at most 4, and only when two distinct signals of one
   family appear). Generic words (resistor, capacitor, timer, latch) identify nothing from rows.
   A title therefore always outweighs what a features list happens to mention; below 2 points
   the part stays unclassified. The saved decision says where the family was read.
8. `MakeModelRequest.family` (`--family` on `model build`) lets the operator name the family
   when the evidence does not. It never unblocks a class, never makes a part supported, and is
   recorded as declared. `--engine` is exposed on `model build` with the same three values.

## D-056 — A second behavioural implementation, and the route seeds from whichever implementation matched (2026-09-28)

1. `models/op_amp.py` is the second registered implementation: a dual op amp on the pin shell.
   Its typed design (`OpAmpDesign`) is read from cited rows only. For each input it takes the typical
   value of a channel-1 row that is bound to an op-amp probe and whose citation verified; a number
   nothing cites is a labelled template default. Essential inputs: input offset voltage, input bias
   current, open-loop gain, gain bandwidth, slew rate, high-output headroom, low output voltage and
   supply current per amplifier. Output current limit, output resistance and offset current are
   non-essential defaults. It matches only the eight-pin dual pinout that the op-amp probes drive,
   and only with a row bound to an op-amp probe, so no claim is made for a pinout the independent
   tests cannot exercise.
2. Supported means what it means for the buck: every essential input cited, and each of seven
   essential behaviours (offset, bias, gain, bandwidth, slew rate, output swing high and low, supply
   current) covered by a bound row. Removing one typical value or one bound row withdraws the claim.
3. `Implementation` now carries `seed`, `template` and `heading`. The behavioural route seeds from
   the implementation the support decision named. The agent route still seeds only the buck
   template. The provenance file, the model-card heading and the timing route name follow the
   implementation, and a design has `record(delivered)` tying it to the delivered bytes.
4. The pin shell needed no change. Rendered from the frozen LM358 rows the design reproduces the
   values the hand-built reference part was judged with; the viability gate finds no failing check
   and says UNKNOWN, not PASS, for the short-circuit limit, because the rows carry no such number.
5. Citation verification now accepts a page when either of two independent PDF readers (pypdf and
   pdfium) contains the excerpt. Found because the frozen LM358 excerpts were read by pdfium
   ("VC M = 0 V") and pypdf now spaces the same text differently ("V C M = 0 V"), so every LM358
   citation failed to verify and the honest gate refused the part. The comparison is unchanged
   (whitespace folding, no fuzzy match); each reading is compared on its own, never across the
   break between them. The second reading costs about 0.5 to 0.7 s per datasheet.

Limits: one part per family is demonstrated, because the only extracted specs on this machine are
TI's TPS54332 and LM358. The op-amp probes fix the eight-pin dual pinout, so a single or quad op amp
stays outside `behavioral` until the probes take their ports from the pin roles.

## D-057 — Pin-only mode, open-drain pins in the gate, a wider block list, and a coverage record (2026-09-29)

1. `--engine pin_only` is a real route, requested by name and never chosen for the caller.
   `models/pin_only.py` turns the extracted pin table into a model on the pin shell: kinds come from
   the pin directions (power to supply, ground to ground, an exposed-pad name to pad); one rail and
   one ground (a table with two supply domains is refused as `pin_only_multiple_rails`); the rail is
   the supply pin of the named domain or one named like VIN/VCC/VDD; a power pin named like an
   output (VOUT, SW, PH) is an inert output; a required exposed pad or second ground gets the
   missing-connection alarm. It models no function: outputs stay high impedance until an instance
   parameter commands them and no datasheet row is judged, so the status is UNKNOWN, never PASS, every
   row reads UNKNOWN or not applicable, and the model card carries the limits and the choices the pin
   table did not settle. The pin table is the one thing it cannot check, so the card says where it
   came from and asks for it to be confirmed against the datasheet pinout.
2. The viability gate judges it (benches for current conservation, floating inputs, shorts, supply
   draw, and each claimed alarm proven both ways). A gate failure withholds the model
   (`pin_only_model_not_viable`) and no other route runs. It is refused, not replaced, without a
   pin table, without a ground or supply pin, with two supply domains, or without LTspice.
3. The gate now exercises open-drain pins. `GatePin.open_drain` pulls the pin low into a short to
   the supply (and releases it into a short to ground), so its overload alarm can be proven. Before,
   the only bench drove a commanded pin high into a short to ground, which an open-drain pin can
   never do, so every part with a power-good or interrupt pin failed the gate on an alarm nothing could
   trip. A test proves the alarm both ways.
4. The block list gained the families a scan of 61 typed part numbers found unrecognised (they were
   refused as unidentified, not blocked): Xilinx XC9500 CPLDs, Lattice ECP/ECP2/ECP3/ispMACH,
   Atmel/Microchip programmable logic, PolarFire, SmartFusion2, ProASIC3, Raspberry Pi SoCs, Rockchip,
   Qualcomm, Samsung and NVIDIA application SoCs, Sitara AM6 and OMAP, Kinetis, S32K, i.MX RT, AT89,
   AT91, AT32, XMEGA, and WCH, Nuvoton, STC, Puya, Padauk and 8051 microcontrollers, and hyphen
   tolerant wording (field-programmable gate array, complex programmable logic device, programmable
   logic device, microcomputer, system on a chip). Prefixes are written as narrowly as the lookalikes
   need: MAX3232, TDA2030, CH340, AM26LS31, XC9504, BCM43438 and others stay allowed, with tests.
5. `tools/coverage_matrix.py` writes `COVERAGE.md` and `coverage.json` (the support decision for 61
   parts with its milliseconds, and the real behavioural builds with every stage timed) under
   `docs/evidence/2026-09-28-engine-steps/`. Rows typed in the tool use a typed title and say so.

Limits: pin-only models one rail and one ground and uses shell defaults for drive, quiescent current
and leakage (labelled on the card). Two parts (TPS54332, LM358) are built by behaviour; every other
family reaches at most the limited routes until its rows are extracted and an implementation exists.

Addendum (2026-09-29, after the live runs).

6. The local routes are not stopped by the agent network pre-flight. `behavioral` and `pin_only`
   never call the agent, so the INTERNET ACCESS switch governs them only where they read a datasheet
   through the extraction provider. The LM358 datasheet alone builds by behaviour with the switch off
   (25.4 s, no provider call).
7. AI test planning belongs to the legacy route. A local route binds rows with the reviewed keyword
   table and makes no planning call; `--plan-tests` asks for the planner explicitly. Reason: the
   first live attempt on TPS54331 ran past 11 minutes, which is not a default anyone should pay.
8. A test session may not be pinned by an import. A tool that pins `BOARDMODELER_NO_NETWORK` does it
   in its entry point, and `tests/conftest.py` refuses a run in which importing a test module changes
   it. This was the whole cause of the red automated checks (105 and 49 failures).

Live results that bound the claims above: a charge pump (XD7660) reaches a limited pin-only model in
about 158 s, most of it the extraction call; a 21-pin controller (LM5116) is withheld by the gate
after a 348 s run; a second buck (TPS54331) is refused for missing cited limits and independent tests,
not accepted on the strength of the buck template.

## D-058 — Preserve withheld evidence, count observable calls, and keep unsupported rails blocked (2026-09-29)

1. A pin-only viability failure withholds every model deliverable but still saves
   `pin-only-report.json` with the measured gate checks. Replaying LM5116's saved evidence with real
   LTspice confirmed the report survives a block and no library, symbol or card is delivered. The
   report is diagnostic evidence, not permission to relabel the model PASS.
2. The pin-only shell has one modeled rail. A second required supply terminal is structurally
   refused as `pin_only_required_secondary_supply`, even when the extracted supply-domain fields
   are blank or equal. It is refused before rendering or LTspice because the shell's existing
   ground-referenced required-connection alarm fires on a correctly powered secondary rail. An
   optional auxiliary supply does not itself trigger this refusal. Widening the shell later needs
   real powered/open controls; weakening the alarm to obtain a green result is not an option.
3. In the general edition, `run-timing.json` counts actual HTTP inference attempts at the API
   transport boundary, including retries, rather than inferring calls from author turns or
   extraction tasks. Cached and entirely local runs record exact zero. IBM Bob Shell is opaque:
   after a CLI invocation its vendor-request
   total is `null` with an explicit reason, while the observable shell invocation count is retained.
   The timing route names refusals instead of claiming agent authoring happened.
4. The TPS54331 historical extraction is not edited in place. Its electrical table has VIN UVLO
   3.5 V as a maximum, EN 1.25 V as a typical and 1.35 V as a maximum, and current limit 3.5 A as
   a minimum and 5.8 A as a typical. The operating description says typical VIN UVLO is unspecified.
   This corrects the shorthand at the end of D-057: `UVTH` is a missing VIN UVLO design policy, not
   an uncited EN threshold. A new reviewed extraction must be tied to the exact document hash; a
   maximum must never be silently treated as a typical input. TPS54331 stays unsupported until the
   input policy and independent test bindings are sound. A bounded matcher now reads both
   `current limit` and `current-limit`; an offline replay removes only the ILIM citation gap and
   still refuses the part for UVTH and missing independent VREF/current-limit tests.

## D-059 — Freeze independent qualification, preserve reviewed columns, and bind provenance to delivery (2026-09-29)

1. Buck qualification is reusable production code in `authoring/qualification.py` and
   `authoring/buck_qualification.py`. Freeze its plan from the source requirements and spec before
   candidate authoring. The plan records source, spec and implementation hashes, operating conditions,
   independent circuits and acceptance references. It must not read candidate design parameters to
   define a limit. Changing the candidate requires another observation against the same frozen plan,
   not moving the band to make the candidate pass.
2. The mandatory checklist has sixteen checks. The reviewed nominal slice covers VREF, slow-start
   charge, shutdown supply current and operating supply current. Seven M2 observations and five
   switching-frequency/UVLO/enable checks remain explicit UNKNOWN gaps until their independent
   acceptance work is finished. Optional fault controls are synthetic and excluded from device
   counts. Four nominal PASS rows cannot make `family_qualified` true. The supplemental report does
   not replace or relax the ordinary row harness.
3. Every qualification execution stages immutable candidate bytes and keeps its own plan, model,
   simulator, deck, raw/log hashes and measurements. A changed plan implementation, candidate,
   deck, incomplete simulator output or exceeded bound cannot earn PASS. Per-check simulation is
   bounded to at most 120 s and raw output to 64 MiB; oversized output is retained as incomplete
   evidence. Default qualification runs the four nominal simulations; extra controls and M2
   diagnostic observations are explicit choices.
4. A reviewed TPS54331 profile matches only the exact part and TI SLVS839H document hash. It records
   a partial extraction scope, zero-based PDF pages and printed labels, verbatim verified excerpts,
   source MIN/TYP/MAX values, and quantity/condition identity. VIN UVLO MAX 3.5 V is not a nominal
   value. EN has TYP 1.25 V / MAX 1.35 V; ILIM has MIN 3.5 A / TYP 5.8 A. Missing table cells stay
   absent. Supplied historical extractions and frozen bindings are not corrected in place.
5. Bare TPS54331 does not select D or DDA packaging. Common pins 1–8 and the DDA-only PowerPAD
   requirement are separate machine-readable evidence. An unresolved package marker survives a
   saved-input replay and blocks every publication route; resolving numeric inputs alone cannot
   silently omit the pad. TPS54331 remains unsupported while UVTH, package selection and independent
   behavior coverage remain unresolved. The profile is not whole-datasheet extraction or model proof.
6. Code-built VREF and current-limit fixture completion uses verified, active raw requirements of the
   same part/document, qualified physical pin mappings and cited slow-start charging time. It reads
   no candidate parameters. A ranged VIN condition is not a singleton operating point; contradictory
   point values remain refused. Caller-supplied frozen bindings and LM358's reviewed bindings are not
   replaced. AI planning remains explicit on local routes and cannot bypass the fixture guards.
7. A publication records the delivered library and symbol hashes and pin order. A prior exact typed
   design association cannot survive changed delivered library bytes after a resumed repair. Missing
   typed provenance is explicitly unavailable; parameter-origin records describe the seed unless
   the final library still matches it. Publication preserves the candidate's bytes and requires the
   ordinary harness report to match both those bytes and the frozen spec. Supplemental qualification
   receives the exact delivered library and its exact typed-design hash when available. On a reused
   output directory, old app-owned deliverables move into `build/publication-history/`; refusal must
   not leave an old library or card presented as the current result. These hashes establish identity,
   not electrical PASS. Resumed buck/op-amp payloads must parse and render again, matching saved
   schema/type/design/library hashes and current part/subcircuit/spec before retaining exact.
   Invalid provenance must not supply an exact design hash to qualification.
8. The CLI, window and new `MakeModelRequest` API promote `behavioral` to the new-run default.
   Historical saved requests without an engine continue to decode
   as `legacy_ai`. Legacy authoring and pin-only generation stay explicit choices, never fallbacks.
   First-page device identity can refuse a blocked MCU/FPGA/CPLD/processor/SoC before a family hint
   can classify it as supported; application examples that mention those devices do not establish
   the identity of the modeled part. Provider accounting retains D-058's observable-attempt rule.
9. A saved DOCUMENT row's `citation_verified` flag cannot certify itself on replay. Persist the
   current document/page verification result before freezing new requirements or qualification
   sources. Missing source text, failed excerpts and absent verifier results clear stale true flags;
   non-document origins and historical input artifacts remain unchanged. Qualification must retain
   a gap when a required current citation is unverified.
10. This milestone does not complete M6's source-backed package/symbol confirmation gate for
    PIN-01/02/05. Library/symbol hashes, recorded terminal order and the TPS54331 unresolved-pad
    guard establish narrower invariants. Package selection, symbol numbering and discrete pin
    order still require the planned evidence/confirmation gate before publication.
11. Supplemental qualification BLOCKED is publicly BLOCKED with its refusal reason. Fixed FAIL
    remains FAIL; fixed UNKNOWN downgrades an ordinary PASS. Incomplete or unavailable required
    simulation cannot become PASS.

Evidence: local general-edition `runs/qualification-acceptance/acceptance.json` records the same
plan SHA-256 `548871ecfa2b5ef595b85294ed3fc0cefbc7366b9d4a78185e64a0ed1dbf8234` for clean and
VREF=0.72-V candidates. The clean run has 4 PASS / 12 UNKNOWN; the changed candidate has 3 PASS /
1 FAIL / 12 UNKNOWN against the unchanged 0.772–0.828-V reference band. Controls-enabled elapsed
times were 87.497255 s and 83.232343 s. Focused source-column, missing-citation, package-replay,
fixture, wrong-candidate, provenance and callback tests accompany the code. This is a bounded
source-engine milestone; the remaining mandatory checks and broader family coverage are unfinished.

## D-060 — Check first-page device identity and executable buck test eligibility (2026-09-29)

1. A device-class refusal must survive a first-page section boundary. Check identity-bearing
   Features, Description and Overview text in addition to the heading before accepting an
   implementation or declared family. Do not classify an ordinary analog device as an MCU or FPGA
   solely because its applications or power-rail descriptions mention one.
2. A row's statement plus any nonempty probe name is insufficient evidence that an essential
   buck behavior is independently testable. Require an operating numeric reference, source
   page/excerpt, and a valid circuit-measurement recipe with condition evidence, the exact
   physical terminal set, matching units and a compatible operation. Generic regulator probes do
   not describe this physical buck's power stage merely by sharing a behavioral name.
3. This gate establishes test eligibility, not measured accuracy or full-family qualification.
   Preserve explicit missing-test reasons and the independent frozen harness verdicts. Do not
   use these safeguards to claim completion of M6 or the remaining M4b2/M4b3 checks.

Rationale: reproductions admitted an unlisted MCU whose identity followed a Features heading,
and accepted unrelated probe assignments as coverage for every essential buck behavior.
Both bypasses could allow authoring or support claims without the intended evidence.

Evidence: `tests/models/test_support.py` covers blocked identities after section headings,
incidental application mentions, unrelated probes, malformed recipes, missing conditions/limits,
wrong terminals, units and operations. The focused support/pipeline command recorded at the top
of STATUS passed 284 tests in General and 283 in Bob, with one skip and two deselections in each.
Saved TPS54332/LM358 support decisions remain accepted; unrelated-probe variants are refused.
These checks add no simulator measurements or new electrical-accuracy claims.

## D-062 — The window stays; the terminal-only front end is withdrawn (2026-09-30)

The owner asked to go back after the terminal setup and text menu, merged into `main` at dad0d54,
replaced the desktop window: the owner wants the window. `main` again holds exactly the files it had
before that merge, so the window and `Install.exe` are the front end. The terminal work stays on the
`terminal-app` branch and is not merged again without the owner's explicit OK. Because `main`
reverted a merge, git treats those commits as already merged; merging them later needs this revert
reverted first.

The owner's installation goal: the application self-contained in its folder, with the window, and no
Windows warning when the download is first run. That warning comes from Windows for any unsigned
program marked as downloaded from the internet; the application does not produce it.

## D-063 — Require source-backed pinout confirmation before model publication (2026-09-30)

1. Publication must compare the selected package or explicitly equivalent pinout group and every
   physical pin number/name against confirmed evidence, then compare that ordered mapping with
   the subcircuit ports and symbol `SpiceOrder`. A self-consistent library/symbol pair does not
   establish agreement with the device. Missing, ambiguous or conflicting evidence blocks
   publication and must leave a reason.
2. The initial reviewed scope is TPS54332DDA and the base LM358 eight-pin electrical pinout.
   TPS54332DDA uses DDA pins 1–8 plus PowerPAD 9; the pad-to-GND7 connection stays external.
   LM358's D, DGK, P, PS and PW packages form an explicitly identified pinout-equivalent group.
   Confirming that group does not select a package body, PCB footprint or package variant outside
   the group. Aliases are accepted only through the reviewed role mapping.
3. The reviewed profiles use two distinct TI primary documents per part: its datasheet and an
   application or EVM schematic. They are independent documents from one manufacturer. Record
   revision, URL, printed and zero-based PDF page, figure/table, complete PDF SHA-256 and reviewed
   page-image SHA-256; do not confuse two renderings of one document with two sources.
4. TPS54331 remains blocked until the actual D/DDA package and its complete required terminals
   are resolved. Discrete devices without reviewed terminal-order confirmation remain blocked.
   This initial scope does not establish all-family or all-package M6 coverage. Historical outputs
   remain historical evidence; reopening them must not fabricate confirmation for a new publication.
5. Pinout confirmation establishes wiring identity only. It cannot create an electrical PASS,
   relax frozen test limits or complete M4b2/M4b3. Measured results must still refer to the exact
   delivered candidate, and required external ties must remain external.

6. Freeze `spec/pinout-contract.json` before authoring. A source-only receipt is
   `SOURCE_CONFIRMED` with `publication_allowed=false`; only the final exact library/symbol
   check may produce `CONFIRMED` and permit publication. Require physical number, ordered model
   position and symbol `SpiceOrder` to agree. Recheck the source, spec, extracted map and saved
   contract on repair/resume as well as first publication. Archive previous app-owned receipts
   when a new build starts; retain a current BLOCKED `pinout-report.json` when no model is delivered.

Evidence: [M6 report](evidence/2026-09-29-m6-pinout/REPORT.md) records the exact primary documents,
image hashes, implementation boundary and observed acceptance. Both editions' new LM358 runs
retain 32 PASS / 10 N/A with confirmed exact artifacts; TPS54331 remains BLOCKED before source
confirmation and delivers no model. Final buck replay and full-suite evidence are pending.
