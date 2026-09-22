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
