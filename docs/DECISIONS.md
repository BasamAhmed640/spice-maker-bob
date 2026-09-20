# DECISIONS

Every decision that constrains later work. Format: id, date, decision, rationale,
rejected alternatives, evidence. Append-only; a superseded decision keeps its id
and gains a "Superseded by" line.

---

## D-001 — Python 3.14 pin

**Date:** 2026-09-18
**Decision:** `requires-python = ">=3.14,<3.15"`; `.venv` created with
`uv venv --python 3.14`; build backend hatchling; console script
`boardmodeler = "boardmodeler.cli:main"`.
**Rationale:** the machine has exactly one interpreter (`uv python list` reports
only 3.14.2) and every required wheel resolves for cp314.
**Rejected:** CPython 3.13 (would need an extra download for no benefit).
**Evidence:** `uv sync --all-extras` completed; installed versions observed:
numpy 2.5.2, pydantic 2.13.5, pypdf 6.19.0, pypdfium2 5.13.0, keyring 25.7.x,
PySide6-Essentials 6.11.2, spicelib 1.6.3, pytest 9.1.1, ruff 0.16.8,
pyinstaller 6.22.3, reportlab 5.0.1, psutil 7.2.2.

---

## D-002 — Waveform reader backend

**Date:** 2026-09-18
**Decision:** the native reader (`simulation/raw.py`) is authoritative. `spicelib`
(optional `sim` extra) is selected only when it reproduces the native read of the
smoke `.raw` within 1e-9 relative; otherwise the backend stays `native` and the
reason is reported by `boardmodeler doctor`.
**Rationale:** one reader must own the honesty of every measurement; an optional
library must never silently change what "measured" means.
**Rejected:** always using spicelib (adds a dependency to the critical path and
hides layout changes); never installing it (loses an independent cross-check).
**Evidence:** `simulation/backend.py::probe_backend` runs on the smoke `.raw`
produced by `smoke_test`; `doctor --json` reports `reader_backend`,
`spicelib_version`, `max_deviation` and the compared variable list.

---

## D-003 — First regulator part and model provenance

**Date:** 2026-09-18
**Decision:** the first regulator is the **TPS54320** (Texas Instruments), and the
type-A fixture is its **unencrypted PSpice transient model** (`SLVM451A`),
mechanically ported to LTspice. No fallback to `LT8609S` was needed.

**Rationale / observed evidence:**

|Step|Observation|
|---|---|
|Datasheet|`https://www.ti.com/lit/ds/symlink/tps54320.pdf` → HTTP 200, 1 678 941 bytes, sha256 `480b1cdb92b4668e…` (SLVS982C, 43 pages, embedded text)|
|Model package|`https://www.ti.com/lit/zip/SLVM451` → HTTP 200, 79 758 bytes, sha256 `ae5d9bc8128ea8c4…`; contains `TPS54320_TRANS.lib` (27 372 bytes). The product page lists it as "TPS54320 **Unencrypted** PSpice Transient Model Package (Rev. A)" — unencrypted, so a source-level port is possible at all. The average model (`SLVM896`) was fetched too, for reference.|
|Port|`models/adapt.py` applies 5 mechanical transforms (16 recorded changes) and the ported model **simulates and regulates** in LTspice 26.0.0.3|
|Probe result|start-up settles at **3.1324 V** against the application's 3.2691 V divider target (1 k / 3.24 k, Vref 0.8 V) — inside the 5 % probe criterion|
|Licence|TI model terms: provided "as is" for TI parts. Stored under `fixtures/regulator/tps54320/originals/` (**git-ignored**), never redistributed; the committed artifacts are `provenance.json` (URLs, statuses, sha256, licence note) and `adaptation.json` (the change list)|

**Rejected:** the pre-decided `LT8609S` fallback (not needed, and `analog.com`
timed out from this machine during the fixture probe, so its datasheet was not
obtainable anyway); a hand-written behavioural stand-in for the vendor part (the
type-B template exists separately and is *not* a substitute for vendor evidence).

---

## D-008 — The PSpice → LTspice port recipe

**Date:** 2026-09-18
**Decision:** vendor models are adapted only through
`models/adapt.py::port_pspice_to_ltspice`, which enumerates every change and is
covered by tests. The five transforms, each empirically required by the real
TPS54320 model:

|#|Transform|Why (observed)|
|---|---|---|
|1|fold PSpice `+` continuation lines into logical cards first|collapsing `VALUE { {…} }` before folding left a stray `}` on a continuation line → `Unknown parameter + }`|
|2|`VALUE { {expr} }` → `VALUE={expr}`|PSpice ABM syntax; LTspice wants a braced `VALUE=`|
|3|`VSWITCH(Roff,Ron,Voff,Von)` → `SW(Roff,Ron,Vt,Vh)` with `Vt=(Von+Voff)/2` and **`Vh=|Von-Voff|/2`**|LTspice's `Vh` is the **half**-band. Using `|Von-Voff|` doubled the band, so the model's 1 V internal logic never crossed the threshold: the oscillator never toggled and the converter never switched (observed: `V(ph)` flat, `V(vout)` = 0 V)|
|4|strip the `Vdc` unit suffix|LTspice rejects `0Vdc`|
|5|numerical help: no `.tran … uic`, `method=gear`, `trtol=10`, `gmin/abstol/vntol` relaxed, and a diode emission coefficient raised from `N=0.01` to `N=0.1`|with `uic` LTspice reported `Convergence Failure: Time step too small; initial timepoint: trouble with instance "xu1:D_U8_D12"` and never advanced; solving the operating point instead converges in 0.8 s. `N=0.01` is a PSpice idiom LTspice warns about ("Emission coefficient, N=0.01, too small")|

**Consequences recorded in the artifact:** the adaptation report states that diode
clamping differs from the PSpice original and that decks must not use `uic`; the
capability probes below never claim behaviour beyond what they measured.

**Cost note:** the ported transistor-level model needs ≈30 s of wall time per
simulated millisecond on this machine, so probe windows are scaled
(`ModelProbeSpec.probe_time_scale`) and every probe window is expressed as a
fraction of `tstop`.

---

## D-009 — What a capability status means

**Date:** 2026-09-18
**Decision:** `supported` requires a probe that ran and met its numeric criterion;
`unsupported` means structurally impossible or a declared exclusion; `unknown`
means the probe ran but could not establish the behaviour (the observed
measurement is recorded); `not_tested` means no probe exists, a prerequisite
failed, or the probe could not run. **Every one of these blocks a dependent
requirement** — `behavior_gate` gates on `!= "supported"`, so a "not tested"
behaviour can never produce a PASS.

**Evidence:** `models/capability.py`, `tests/models/test_capability.py`
(a raising probe and a cancelled probe both stay `not_tested`; the gate blocks
`unknown`, `unsupported` and `not_tested` alike).

---

## D-010 — Which model carries which claim

**Date:** 2026-09-18
**Decision:** the **generated type-B template** (`BM_REG_BUCK`/`BM_REG_LDO`) carries
the verified dynamic behaviour (startup, enable, power-good, current limit, the
feedback-divider dependency) and is what the demo board and the dynamic tests
exercise. The **ported vendor model** (type A, D-003) is the vendor-evidence
artifact: it is stored, adapted, probed, and reported through its capability
record, and it is exercised by an opt-in test rather than the default suite.

**Rationale — the observed behaviour of the vendor model on this machine:**

|Observation|Value|
|---|---|
|first isolated start-up probe (1.2 ms window, `tmax=100 ns`, `Css=1 nF`)|converged in 57 s, output 3.1324 V against a 3.2691 V target|
|8-probe capability sweep (2.1 ms windows via `probe_time_scale=0.35`)|389 s total; `shutdown`, `dc_regulation`, `input_current`, `reverse_current_prebias` = supported; `startup` timed out after 300 s (exit 1 then hang); `switching_waveforms` errored (a deck bug, since fixed); `load_transients`/`current_limit_recovery` = unknown (probe could not establish them)|
|Phase 2 application test (2.1 ms window, 10 Ω load)|**hit the 600 s cap** without finishing|

So the model is *usable* but not *reliably fast*: probing it is evidence, running a
multi-millisecond board scenario on it is not. Making the default test suite depend
on it would turn every run into a coin flip on convergence, which is a worse
failure mode than an explicit, recorded limitation.

**Consequences:**

* the capability record for the vendor model is exactly what the probes measured —
  it must not be "improved" by re-running until it passes;
* requirements mapped to an unprobed vendor behaviour are gated to UNKNOWN
  (D-009), so nothing downstream can quietly rely on it;
* `tests/regulator/test_first_regulator.py` documents this split at the top of the
  file, and the broken-divider requirement is verified on the type-B model, whose
  own capability record is probed and exported like any other model.

**Rejected:** deleting the vendor artifact (it is the type-A evidence the plan
asks for, and its `dc_regulation`/`input_current`/`shutdown` probes are real);
loosening the timeouts until it passes (that would hide a machine-dependent
property behind a number chosen to make a test green).

---

## D-006 — Simulator invocation

**Date:** 2026-09-18
**Decision:** all simulator invocations are
`LTspice.exe -b [-ascii (extra switches...)] <absolute deck path>` with
`cwd = <run directory>`; output discovery is `<stem>.raw`, `<stem>.log`,
`<stem>.op.raw` beside the deck. `-I<path>` is **never** passed. `.step` is
**never** used (one deck per corner instead).

**Rationale (measured on LTspice 26.0.0.3):**

|Observation|Evidence|
|---|---|
|`-b <deck>` exits 0, leaves `<stem>.raw`/`.log`/`.op.raw`/`.db` beside the deck|`tests/ltspice/test_invocation.py::test_run_batch_argv_and_artifacts`|
|`-b <deck>.asc` simulates **directly** (no `-netlist` pre-step)|`test_batch_simulates_asc_directly`|
|a deck with an undefined subcircuit exits 1 and writes the error into `.log` (no dialog)|`test_run_batch_reports_failure_for_bad_deck`|
|`-I<dir>` does not resolve the subcircuit and leaves the process alive on a modal dialog (90 s timeout, repeated 3x at 25 s)|Phase 0 probe, `build/scratch` session log; this is why `run_batch` has no `search_paths` parameter|
|passing `-I` *last* (as the shipped help implies) returns exit 1 with "This sub-circuit name is not defined"; passing it first returns 0 once and then times out — nondeterministic ⇒ unusable|same probe|
|generated `.asy` + `.lib` **beside** the `.asc` resolve with no `-I`, and LTspice auto-emits `.lib <model>` for the symbol's `SpiceModel` attribute|`test_local_symbol_and_model_resolve_without_search_path`|
|`LTspice.exe -version` prints `26.0.0` and exits 0 in 0.2 s|`smoke_test(...).version`|
|`.step` concatenates every step into one `.raw` whose header carries no step index|Phase 0 probe (`No. Points: 3139` for 3 steps, flags `real forward stepped`)|

**Rejected:** `-I` search paths (nondeterministic hang); `.step` (unreadable step
boundaries); waiting forever for process exit (a modal dialog after the log's
`Total elapsed time` line is indistinguishable from a hang — the watchdog kills
the tree we spawned and records `terminated_after_marker`).

**Watchdog:** the process tree of the spawned PID is killed after
`marker_grace_s` (5 s) past the log's `Total elapsed time` line, or at
`timeout_s`. Only the tree of our own PID is touched, never another LTspice
instance the owner may have open.

---

## D-007 — `.raw` payload formats

**Date:** 2026-09-18
**Decision:** the reader detects the payload layout from the file size and the
header, never from an assumption. Supported: header text in UTF-16LE (LTspice 26)
or ASCII; binary payloads with stride
`8 + 4*(nvars-1)` (variable 0 is a double, the rest float32), its 8-byte aligned
variant, `float32`, `float32-align8`, `float64`; and `-ascii` text payloads.
Stepped and complex payloads raise `RawFormatError`.

**Rationale:** LTspice 26 writes a **UTF-16LE** header (no BOM) where older
releases wrote ASCII, and stores non-time variables as **float32**, so a binary
`.raw` cannot support assertions tighter than ~1e-7 relative. ASCII output keeps
~10 significant digits and is therefore *more* precise than the default binary
file. Measurement code must not assume double precision for signals read from a
default `.raw`.

**Rejected:** guessing the layout from the flag word (a wrong guess would silently
produce garbage); zero-filling an unrecognised payload (forbidden).

**Evidence:** `tests/ltspice/test_raw_reader.py`: closed-form RC/divider checks,
ASCII-vs-binary agreement (measured 1.2e-9 for `V(in)`, 2.9e-8 for `V(out)`,
bounded at 1e-7 in the test with the float32 explanation), truncated/complex/
stepped payloads raising, and a synthetic file whose stride is derived from size.

---

## D-005 — HTTP inference endpoint (DeepSeek) and Bob

**Date:** 2026-09-18 (verified against the vendor documentation during Phase 4)
**Decision:** `providers/http_inference.py` is an OpenAI-compatible chat-completions
adapter that ships **no default endpoint and no default model**: both must come from
configuration, so nothing can be invented or silently inherited. The adapter's shape
is modelled on the strings published at `https://api-docs.deepseek.com/`, verified by
fetching that page during this session:

|Parameter|Documented value (observed 2026-09-18)|
|---|---|
|base_url (OpenAI-compatible)|`https://api.deepseek.com`|
|chat path|`/chat/completions` (the published `curl` example posts to `https://api.deepseek.com/chat/completions`)|
|model names|`deepseek-flash`, `deepseek-v4-pro`; the legacy names `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are still accepted for retired models|
|auth|`Authorization: Bearer <key>`|

`tests/providers/test_http_inference.py::test_deepseek_documentation_still_names_the_configured_strings`
re-checks that the documentation still publishes `api.deepseek.com` and
`chat/completions`; it is marked `network` **and** requires
`BOARDMODELER_NETWORK_TESTS=1`, so the default suite makes no outbound call.

Note for anyone reading an older draft of this file: the model names
`deepseek-chat` / `deepseek-reasoner` are no longer the documented names, which is
exactly why no default is compiled in.

**Bob.** `providers/bob.py` implements `BobDirectProvider` and `BobShellProvider` but
this machine has no `BOB_*` credentials and no verified approved endpoint, so the
direct path reports `BLOCKED("bob_credentials_unavailable")` and is **never**
substituted by another provider. Bob Shell additionally requires both
`policy.allow_bob_shell` and `--allow-bob-shell`, refuses documents classified
internal/confidential/unknown, and runs through `security.subprocess_guard`. It is
recorded as unexercised rather than as "supported".

---

## D-011 — A scenario must apply the stimulus it declares

**Date:** 2026-09-18
**Decision:** every `ScenarioSpec` in `verification/scenarios.py` that the demo board
runs must have its declared stimulus injected into the deck it is checked with; a
scenario whose stimulus cannot be injected is reported as not applied, with the
reason, instead of being checked under a deck that does not contain it.

**Rationale:** `deck_for_scenario` originally varied the deck only for `fast_rail`
and `slow_rail`, so 21 of the 23 scenarios were checked against the *nominal* deck
under a different name. Every one of those results would have been a claim about a
stimulus that was never applied — the precise failure mode this project exists to
avoid, and one that is invisible in the report because the scenario id looks right.

**Consequences:**

* the built project carries `tests/scenario_stimulus.json` mapping each scenario to
  the exact injected card(s) or to `applied: false` with a note;
* `tests/pipeline/test_demo_decks.py` asserts each scenario's deck differs from the
  nominal deck (or is recorded as not applied) without needing a simulator;
* the reference clock is a stand-in whose rate is not asserted by any requirement
  (`REQ_DEMO_CLK_009` is an ASSUMPTION), and each deck says so, because driving a
  100 MHz clock with 1 ns edges across a 10 ms window forced about 1e6 timesteps and
  a 120 MB `.raw` for a property nothing checks.

**Rejected:** keeping the shared deck and documenting the shortcut (it would make the
report's per-scenario statuses unverifiable); shrinking the scenario list to the two
injected ones (the plan fixes the scenario ids, and the ones that matter — clock,
straps, reset, power-good — are exactly the ones that were missing).

---

## D-012 — The demo board's own defects, and which source owns the timing

**Date:** 2026-09-18
**Context:** wiring the scenario stimuli (D-011) and the fault matrix exposed four
defects in the demonstration itself, each of which had made a claim unfalsifiable.

1. **The reset supervisor had the polarity of a power-good *pin*.** `U3`/`U4` were
   wired as `BM_PG`, whose documented behaviour is to *assert its output when the
   sensed level is good*. A reset output must do the opposite: hold low while power
   is bad and release after it is good. Measured consequence: `PERST#` tracked the
   3V3 rail up at 2.05 ms, before either power-good pin was valid, and was pulled low
   2 ms *after* power was good — the exact inverse of the fixture's own contract.
   Fixed by adding the primitive the circuit actually needs: `BM_RESET_SUP`
   (`pg_in out vdd vss`, `VTH/VHYS/TD`), same shape as `BM_PG` with inverted
   polarity, documented as such in `models/primitives.py`. Measured after the fix:
   PG_1V8 valid at 2.849 ms, `PERST#` released at 5.629 ms — a 2.78 ms hold, inside
   the required 1 ms to 100 ms band.

2. **The deck carried its own copy of the fixture's timing.** `loads` and `timing`
   in `circuit/project.json` declared the load steps and the reset delay, while the
   deck builder used module constants. A mutation of the declared value therefore
   changed nothing the simulator saw — `release_reset_early` was inert, and
   `slow_rail_u2`'s "1V8 overload" never reached the deck. Fixed by making the
   project the single source: the deck reads `timing.load_step_*`, `loads` and
   `timing.pg_delay_s` from the circuit it is building for. The fault matrix now
   detects both mutations.

3. **Two requirements were missing entirely.** Nothing asserted that the switch's
   strap pins are on the nets their levels are read from, and nothing asserted the
   `PERST#` pull-up's domain, so a strap *swap* and a reset pull-up re-referenced to
   the 12 V rail were invisible to every check (the level measurements cannot see
   either: the strap levels are unchanged by swapping which pin reads them). Added
   `STRAP_011` (pin→net for all three straps) and `RESET_012` (the reset line idles
   at the 3V3 level and never above it). Both are dynamic, so they fire on the deck.

4. **A load step inside a converter's soft start is a different experiment.**
   Stepping the 1V8 load at the instant its LDO starts dragged that rail to −20 V,
   and a load step landing inside a requirement's steady window reported the
   fixture's own stimulus as a rail violation. The load steps now land after each
   rail is regulating, and the values live in the fixture.

**Deliberately left failing.** With those fixed, three of the ten scenarios report
FAIL: `staggered_rails` and `load_step` dip the 3V3 rail to 3.126 V/3.130 V against
its declared 3.135 V floor when a load steps, and `brownout_short_interrupt`
collapses during the dip. A fourth, `pullup_missing`, is UNKNOWN: its removal
isolates the sideband node so LTspice drops it from the `.raw` and the level
requirement cannot be evaluated, and it is reported with that reason. These are
reported as findings about the fixture and the reduced models.
Widening the ±5 % window, or omitting the load until the number flips, would be
choosing the answer rather than measuring it — which is the one thing this project
must not do.

**Evidence:** `uv run boardmodeler demo build --out build/demo` → 30 requirements,
10 test cases, 23/23 stimuli applied; `uv run boardmodeler run mutations` → 7/7
detected with the original project byte-identical. The full-suite and
scenario-check results are recorded under "Commands run" in `docs/STATUS.md`.

---

## D-013 — The three circuit FAILs are one model-fidelity finding, not three board faults

**Date:** 2026-09-18
**What was reported:** `staggered_rails`, `load_step` and `brownout_short_interrupt`
FAIL the 3V3 rail-window requirement, and `pullup_missing` is UNKNOWN. A reader
reasonably asks whether the board is broken. It is not, and the measurements below
are why the number is misleading as it stands.

**Measured, on the built demo project with real LTspice:**

|Variant|min V(3V3) in the 4–10 ms window|Floor|
|---|---|---|
|`load_step` baseline (0.5 A step at 6 ms)|2.9107 V|3.135 V|
|the same deck with the extra step removed|**3.2703 V** (in window)|3.135 V|
|the same deck with the step at 0.05 A instead of 0.5 A|**3.2693 V** (in window)|3.135 V|
|buck error-amp gain ×10 (`GM=50` → `GM=500`)|2.9107 V (unchanged)|3.135 V|
|1V8 nominal load removed|2.7026 V (worse)|3.135 V|
|output capacitance ×10|−5.79 V (worse)|3.135 V|
|`BM_LOAD` given a supply-validity gate|2.8921 V|3.135 V|

**Reading:** the excursion appears only in the window that contains a *hard current
step*, scales with the step amplitude, is untouched by loop gain, and gets worse with
more output capacitance. That is the large-signal step response of a **reduced
behavioural model whose compensation is a template constant**, not a demonstrated
rail-margin failure of a real converter (a switching regulator responds within a few
switching cycles; this template has no switching stage at all). The brownout FAIL has
the same shape: a sagging input with a constant-current load against a model whose
protections latch.

**The actual defect this exposes:** the demonstration asks a *transient-margin*
question of a model whose transient response was never established, and the project
already has the right mechanism for that — the capability gate (D-009). It was bypassed
because `build_demo_project` was never given a `workdir`, so **no capability records
were produced for the board's generated models** and nothing could be gated. A
model-limited transient therefore surfaces as a circuit FAIL, which overstates what
was learned in exactly the direction this project exists to avoid.

**Fix (next action, not yet implemented):** probe the board models during the build so
`models/capabilities/*.json` exists, bind the rail-window requirement's transient
behaviour to `load_transients`, and let the gate turn these three results into UNKNOWN
with `model_capability_unsupported` until the template's step response is validated
against a declared criterion. The steady-state window check stays as it is: with the
step removed it passes at 3.2703 V, so it is a real and passing claim.

**Rejected:** widening the ±5 % window, or shrinking the step until it passes — both
choose the answer instead of measuring it. Rejected too: deleting the scenarios; a
scenario that cannot be concluded is exactly what UNKNOWN is for, and the FAIL is
evidence that the gate is missing rather than evidence about the board.

---

## D-014 — Agent-authored models, judged by a frozen datasheet harness (the product pivot)

**Date:** 2026-09-18. **Status:** implemented (`boardmodeler model build|test|install`).

**What the owner actually asked for:** "give it a part number and a datasheet, spin up
agents to make a SPICE model that is thoroughly tested and saved, so I can go into
LTspice and wire it up." The delivered product had drifted: the AI read the datasheet
into requirement rows, but the *model* was a hand-written template library
(`BM_REG_BUCK`/`BM_REG_LDO`) whose parameters were typed by hand, and the "agent"
never authored SPICE. The board demonstration, the circuit checker and the desktop UI
are all outside that ask.

**Decision.** Add one path and make it the front door:

```
boardmodeler model build --part <PN> --subckt <NAME> --requirements <json> \
    --bindings <json> --out <dir> [--iterations N]
```

1. The datasheet's extracted rows become a **frozen spec** (`<workdir>/spec/characteristics.json`):
   requirement id, limit, unit, page, verbatim excerpt, and the probe bound to it.
2. An **agent authors** `<workdir>/model/<SUBCKT>.lib` and `<SUBCKT>.asy`. Nothing else
   the agent writes is used, and the spec directory may not change — `build_model`
   re-hashes it after every turn and stops with `UNKNOWN(spec_tampered)` if it did.
   Tolerance relaxation is therefore impossible by construction, not by instruction.
3. A **deterministic harness** runs one probe deck per bound characteristic through
   real LTspice and compares the measured value to the cited limit. No measurement →
   `UNKNOWN` with a reason. A characteristic no probe can reach keeps its
   `not_testable_reason` on the card.
4. Deliverables: `<SUBCKT>.lib`, a symbol whose `SpiceOrder` bijection is validated,
   `example.cir`, `harness-report.json`, and `MODEL_CARD.md` — every number on the card
   comes from an observed run, and unmeasured rows are listed as declared gaps.

**Agent backend: IBM Bob Shell**, chosen by the owner. Interface verified against IBM's
own documentation on 2026-09-18 (recorded here because D-011 requires endpoint/CLI
strings to come from official docs, never invention):

|Fact|Source|
|---|---|
|Install (Windows)|`powershell -c "irm -Uri https://bob.ibm.com/download/bobshell.ps1 \| iex"`; Node ≥ 24 (`/docs/shell/getting-started/install-and-setup`)|
|Non-interactive run|`bob run [options] [prompt...]`, prompt may be piped (`/docs/shell/getting-started/start-bobshell-non-interactive`)|
|Automation flags|`--format json\|stream-json`, `--max-turns <n>`, `--max-cost <amount>`, `--resume <task-id>\|latest`|
|Result object|`{type:"result", timestamp, status:"success"\|"error", stats:{task_id,total_tokens,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cache_ratio}}`|
|Auth|`BOB_API_KEY`; a key with Scope **Inference** needs nothing else, a **general** key also needs `--team-id <team-id>` (`/docs/ide/account/api-keys`)|
|Skills|YAML frontmatter (`name`, `description`) + Markdown; repo copy at `skills/ltspice-model-author/SKILL.md` (`/docs/shell/features/skills`)|
|Usage accounting|Bob reports **tokens**; the shell path counts runs in **turns** — never converted silently (D-011)|

**Why a frozen harness rather than "the agent tests itself":** an LLM can write a
plausible `.lib` for any part; what it cannot do is decide whether the result matches
the silicon. The harness is the part of the system that can be trusted, so it owns the
limits, the measurements, and the verdicts, and the agent owns only text. This also
keeps the earlier honesty invariants intact: `PASS` still requires an observed
simulator artifact, and a row that cannot be judged is still `UNKNOWN` with its reason.

**Rejected:** (a) letting the agent edit the spec or tolerances — that is the failure
mode the whole design exists to prevent; (b) judging an agent's model against another
model (vendor or template) as the oracle — a comparison between two models says nothing
about the datasheet; (c) generating the model from a template and calling it
agent-authored — the owner asked for authorship, and the template path remains
available as a *seed* the agent may read, not as the answer.

**Termination (owner's rule, 2026-09-18, revised the same day):** *no wall clock ends a
build.* The first cut used a per-turn timeout and a build deadline; the owner rejected time
limits outright — "I want the agents to run until satisfied". The loop therefore ends on
satisfaction or on the agent stalling, and nothing else:

* `PASS` — the harness passes for every covered characteristic;
* `UNKNOWN` — the agent made no progress for `stall_patience` (2) consecutive turns. A turn
  makes progress when the model bytes changed **and** the failing set differs from the
  previous turn's; a failure that changed shape is still motion. The detail names the turn
  count and the probes still failing, and the result carries every row measured so far;
* `UNKNOWN` — a caller-set `max_iterations` was reached. `None` is the default: no cap;
* `UNKNOWN(cancelled)`.

`turn_timeout_s` defaults to `None` (an agent invocation is unbounded); setting it is the
caller's choice, not the tool's policy. The probe set, the per-probe timeout and the number
of judged rows are never reduced for speed — that is the half of the old rule that stands:
bound the agent's repetitions, never the harness's diligence.

**Internet reinforcement (same day):** the owner asked that the internet be searched for
supporting material while a model is made. The stage records errata, application notes and
vendor-model caveats, but only text this tool actually retrieved, stored verbatim with its
URL and hash; an agent's claim we could not fetch is kept as `retrieved=false` with the
reason. Nothing in it can change a status — the datasheet rows stay the only oracle — so it
can inform a reader without ever upgrading a model's claims. The search is cancel-aware
(the build's cancellation event reaches the agent turn) and carries its own budget
(`reinforce_timeout_s`, default 300 s, `None` unbounded); when that budget expires the stage
records `unavailable` with the reason and the build continues. This budget applies to the
search only: the author loop stays unbounded. The fetcher refuses any
destination that is not a public internet host (loopback, private, link-local, multicast
or reserved addresses, and any name that resolves to one of those or does not resolve at
all), and re-checks that policy on every redirect hop.

**Cost of the drift, recorded honestly:** the UI (3.4 kloc), the circuit checker and
schematic layer (4.0 kloc), and the board demonstration were built against the earlier
spec and are not part of this path. They are left in place, dormant, rather than
deleted; nothing in the new path imports them.


---

## D-015 — Two builds from one catalog: API-key agents, and "Spice Maker"

**Context.** The owner asked for three things at once: rename the repository to
*Spice Maker*, ship it as a one-click installer like the Claude/ChatGPT/Slack downloaders,
and make the agent take a **raw API key** — Bob's by default, plus the mainstream vendors —
with no login flow anywhere. Then: two repositories, both carrying that work, one of which
accepts only the IBM Bob API.

**Decision — the agent catalog is the single knob.** `src/boardmodeler/agent_providers.py`
holds an ordered tuple of `AgentProvider` entries. Everything else reads it: the setup
page builds its provider row from it, the backend factory resolves a provider from it, and
`doctor` reports one credential per entry. A build that must accept only Bob ships
`CATALOG = (bob,)`, and then there is *no* provider row on the setup page and the key row
keeps the label it always had (`BOB API KEY`) — the fork's divergence is that tuple plus
README wording, not a fork of the code. `tests/ui/test_setup_dialog.py` proves the
single-entry build behaves that way in-process, so the promise is tested, not asserted.

**Decision — keys, not logins.** Every provider is reached with a raw API key stored in the
OS keyring under `provider:<name>:api_key` (the repo's existing credential helper), with
`BOARDMODELER_<NAME>_API_KEY` and the vendor's own variable as environment fallbacks. Nothing in this path opens a browser, and no key is ever written to
a config file, a project directory, a manifest or a log line.

* **Bob** keeps its documented route: the Bob CLI (Bob Shell), whose own docs state that
  `bob run`/`bob chat` authenticate from `BOB_API_KEY` alone (an Inference-scope key needs
  no `--team-id`; a general key does). The key is passed to the child process through its
  environment and never appears in argv.
* *Bob over plain HTTP was probed and refused, and is therefore not shipped*: on
  2026-09-18 every request to `https://api.us-east.bob.ibm.com/inference/v1/...` from this
  machine answered Cloudflare `403` (bot-management HTML) for `urllib`, `curl` and Bun
  `fetch`, with both `Authorization: Apikey` and `Bearer`. IBM publishes no inference path
  in its docs, only the region host list. A guessed endpoint that the vendor's edge blocks
  is exactly the kind of thing D-005 forbids, so Bob stays on the documented CLI path.
* The HTTP vendors speak their own documented shapes (`openai` chat-completions,
  `anthropic` messages, `google` generateContent). Endpoints and default model ids are the
  vendors' own quickstart values, each entry carrying the doc URL it came from, and the
  model id is editable per machine because those strings drift.

**Decision — what the rename does and does not touch.** The repository is `spice-maker`,
the window/app/installer identity is *Spice Maker* (with a space), and the frozen
executable is `SpiceMaker.exe`. The Python distribution keeps the name `boardmodeler`, the
console script stays `boardmodeler`, and the keyring service (`boardmodeler`), credential
names (`provider:bob_shell:api_key`) and config directory (`%APPDATA%\BoardModeler`) are
unchanged on purpose: renaming them would orphan the API key and settings an existing
install already has, and that is a worse outcome than an internal name that lags the
brand. `docs/STATUS.md` records the state of each surface.

**Rejected.** (a) Deleting the non-Bob provider code here — it doubles
the maintenance of a fork whose whole difference is one tuple, and the tests covering the
HTTP wires would not exist there to catch a regression in the shared code. (b) Shipping the
reverse-engineered Bob inference endpoint as a default — unverifiable from this machine and
undocumented by IBM. (c) Making the setup page a multi-page wizard to hold provider,
model and key — the surface stays one content-sized page (`docs/DECISIONS.md` D-014, and the
forbidden-label test in `tests/ui/test_setup_dialog.py`).

**Addendum, same day (second pass).** Four things were settled after the decision above was written.

* **OpenCode Zen / Go is a catalog entry, not a new wire.** `POST
  https://opencode.ai/zen/v1/chat/completions` with `Authorization: Bearer <key>` — verified against
  the live gateway: no key → `401 {"type":"error","error":{"type":"AuthError","message":"Missing API
  key."}}`, a bogus Bearer key → `Invalid API key`, and the same bogus key in `x-api-key` → `Missing API
  key`, so the scheme is Bearer. Only the models Zen serves from `/chat/completions` are reachable
  through this wire; its `/responses` and `/messages` models are not, and the MODEL row in SETUP is
  where that choice lives. This is the "OpenCode Go option" the owner asked the full-catalog build to
  carry.
* **`model build --provider` names the *agent* provider.** It named the extraction provider before.
  Extraction keeps D-011's own walk over `ProviderConfig.provider_order`, and `--requirements` /
  `--bindings` now reach the datasheet path's request too, so a supplied extraction is honoured there
  instead of silently re-extracting with the fixture provider.
* **A reasoning-first model cannot finish an authoring turn, and the tool says so.** Against the real
  23.8 kB prompt, DeepSeek's two models spent the entire output budget on `reasoning_content` and returned
  an empty `content` (observed three times, at 12 288 and 32 768 tokens, 57–303 s). The transport then
  reported `response_empty … finish_reason='length'` with the truncation named — an honest stop instead of
  a silent retry loop. This is a property of those models, not of the contract, and it is history here:
  the HTTP agent transport itself is gone from this tree (third addendum below).
* **The installer is the application and nothing else.** A Velopack one-click setup with the animated
  pepper splash, Start Menu and desktop shortcuts, and `Update.exe --uninstall --silent`; it carries no
  LTspice, Bob Shell or Python payload (SHA-256 of all 16 606 files under the two raw LTspice trees is
  unchanged across install and uninstall). The freeze excludes the venv's optional weight
  (`spicelib` → scipy/matplotlib, `reportlab` → PIL) and the Qt modules a Widgets application never
  loads: 122.6 MB → 63.1 MB of Setup.exe over a 254 MB → 124 MB payload. The cost is the optional
  `spicelib` reader inside the frozen build, which reports its documented "not installed (optional 'sim'
  extra); using the native reader" — the native reader is authoritative anyway (D-002).
* **Bob stays native.** Bob Shell with `BOB_API_KEY` is the documented consumer of an Inference-scope
  key; the application defaults to that provider, opens no browser, never logs in, and never substitutes
  vendor when Bob is unavailable. This tree is the same code with a one-entry catalog.

**Addendum, same day (third pass).** The owner asked this tree to stop presenting itself as a
trimmed build: the README header no longer announces a restriction, no docstring or comment calls
it one, the window title is plain *Spice Maker — IC model maker* again, and the SETUP page states
the provider it uses (`IBM Bob is the provider this build uses.`) rather than what it refuses. Nothing functional moved: the catalog still holds the one Bob entry, and that
catalog is the only place a provider's key label, credential name and environment fallback are
declared — a sweep of the whole tree finds no other vendor's key variable (`BOB_API_KEY` and the
generic `BOARDMODELER_<NAME>_API_KEY` pattern only), so no other vendor's key handling exists here.

**Addendum, same day (fourth pass) — the other vendors' agent transport is deleted, not just unused.**
The owner asked again, in plainer terms: this tree should be Bob-specific and should not carry other
providers' API-key handling at all. So the HTTP agent transport (`authoring/api_backend.py`: the
`openai`/`anthropic`/`google` wires, their request shapes, their key resolution and the reply parsers —
787 lines plus 1 074 lines of tests) is **removed**, and with it the CLI surface that only existed for it
(`model build --backend api`, `--model`, `--max-tokens`; the backend name is `bob`). What is left is one
agent path and one seam: `authoring/backends.build_agent_backend` resolves the catalog's provider to the
Bob Shell backend, refuses an id this build does not accept **by name**, and refuses a catalog entry whose
transport has no backend (`wire_unsupported`) instead of coercing it. `credential_for`/`env_sources` moved
into the same module, so `doctor` reports exactly the resolution the backend performs. This reverses the
rejection recorded above: that rejection weighed a *fork's* maintenance, and the owner's requirement that
the tree contain no other vendor's key handling outweighs it. What is *not* removed is the extraction
transport in `providers/http_inference.py` and `providers/bob.py`: Bob Direct itself speaks the
OpenAI-compatible chat-completions shape for datasheet extraction, so that code is Bob's own path, not
another vendor's.


## D-016 — Shared, measured model iteration and explicit I/O scope (2026-09-19)

The selected author backend also extracts the supplied datasheet. Four tasks share one
schema/document context, with one correction attempt for invalid structure or semantic
classification. Adapter prompt versions participate in extraction-cache keys. The user's
GO action explicitly authorizes sending the selected document; its unknown classification
is not relabeled public. Internal/confidential policy restrictions remain authoritative.

Keep the best measured model and immutable attempt snapshots. Compare unknown rows,
failed rows, then normalized numeric residual; cycling failures is not improvement.
Reuse only matching validation inputs with intact raw/log evidence, remeasurement and
limit comparison. No UNKNOWN cache reuse, no tolerance relaxation, no false PASS.

Operating conditions and pin maps are part of the frozen spec. I/O coverage is electrical
and conditional; separate operating points get separate decks. Temperature-dependent and
high-speed channel/protocol claims need separate data and engines. Imported vendor files
are byte-preserved, caller-attributed sources, with validation explicitly UNKNOWN.

The two repositories share implementation and tests; build_flavor.BOB_ONLY filters the
accepted catalog. Separate Velopack IDs keep installs distinct. Easy-download ZIPs wrap
the unmodified animated installer as Install.exe; LTspice and Bob Shell remain external
prerequisites. Publishing to each repository's main branch is explicitly user-authorized.
