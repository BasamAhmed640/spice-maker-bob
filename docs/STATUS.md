# API completion release 1.2.1 - 2026-09-20

The streamed HTTP reader now stops at [DONE] instead of waiting for socket EOF,
while the decoder still enforces finish_reason. Metadata uses complete labelled pin
sections without removing any electrical requirements. Bob authoring still uses Bob
Shell; the shared HTTP checks do not establish Bob latency. See API_STREAM_PROGRESS.md.

Validation: full source suite `1090 passed, 8 skipped, 159 deselected` with
`pytest -q -m 'not ltspice and not network'`; Ruff check/format passed. A real localhost
server reproduces the old reader timing out after sending a completed response; the
new reader returns immediately. Five vendor PDFs retain all electrical pages while
metadata input is reduced. No new paid inference call or complete UCC28251 model is
claimed by those checks.

The Bob installer was built and verified by Windows installer run 35558908802
from source commit 45498e61257344d21cd7c5cd60bb18fe80cd0647. Source test workflow
35558908894 also passed. The downloaded installer SHA256 matched the build record
and portable-install verification record. Windows Application Control blocked the
earlier local freeze (4551); GUI/install verification passed on the GitHub runner.

# Portable release 1.2.0 - 2026-09-20

Install.exe now extracts app/ beside itself, preserving this folder's data/ and
models/ on update. New copies require setup and cannot read the previous AppData
settings/key. Python file writes, scratch work, Bob profile and model exports stay
under the extracted root. Details: PORTABLE_STORAGE.md.

Validation observed for this change:
- General source suite: 1186 passed, 4 skipped, 159 deselected (`pytest -q -m 'not ltspice and not network'`).
- Seven new portable-state regression tests passed; setup plus portable tests: 20 passed.
- Bob source suite including portable tests: 1083 passed, 8 skipped, 159 deselected.
- Extraction regression tests after page-shape repair: 11 passed in each edition.
- Ruff check/format passed.
- Both actual animated installers passed extraction, first-launch SETUP, relaunch,
  same-folder update preservation, fresh-copy isolation and external-config rejection.
- Both installed frozen apps passed the real LTspice RC smoke check: 0.632119 V
  versus 0.632 V expected, raw/log hashes captured, exit 0; all run files under data/temp/.
- The recorded UCC28251 responses validate after local page-field normalization;
  no new API call or completed UCC28251 model is claimed.

Historical release notes follow.

# Current status — Spice Maker Bob

Updated 2026-09-20 for the 1.1.3 maintenance build.

The source catalog and author factory contain only IBM Bob. Removed integrations are
not hidden behind a runtime filter: they are absent from the current source. Setup,
errors, tests and documentation use Bob-only terminology. Incompatible saved settings
are refused without displaying the obsolete selection; USE IBM BOB explicitly repairs it.

Shared extraction changes include exact-part prompts and cache identity, safe PDF
permission changes, compact complete JSON requests, and parse-location diagnostics
that redact the entire response before shortening an excerpt. Regression checks cover
cross-part cache isolation and partial-secret alignments.

The 1.1.2 predecessor passed its recorded CI checks and a local packaged GUI launch
check. These are historical observations, not evidence of every UI action or a real
Bob-authored device model. Live Bob qualification requires Bob Shell and an authorized
Bob key. Complete LM358 electrical qualification remains unestablished.

This repository intentionally includes the actual installer in the main source ZIP.
The binary and source are larger as a result. Do not move it to a different download
or to a pointer file: Code → Download ZIP must contain the runnable Install.exe.

Validation of 1.1.3 is recorded below after the actual commands complete.


## Observed 1.1.3 checks

- `.venv/Scripts/python.exe -m pytest -q -m "not ltspice" --tb=short`: 1004 passed, 4 skipped, 128 deselected. Skips identify absent vendor originals.
- Ruff check and format completed successfully.
- `installer/build.ps1 -Version 1.1.3` completed, including a real responsive frozen
  Qt window, animated setup and root/ZIP installer publication.
- Root Install.exe matches the canonical setup and release ZIP installer byte for byte.
  Both splash GIFs retain 90 frames.
- The actual build cleanup code retained 1.1.10 and a different package's artifacts
  while deleting only requested 1.1.1 filenames in an isolated test directory.
- All 233 current tracked/untracked text files passed the Bob terminology scan.
  All 99 compiled application modules in the frozen executable also passed.
- The packaged setup JSON path refused an incompatible setting without displaying its
  old agent/model name. GUI launch was repeated alone to avoid overlapping screenshots.
- Test count decreased because removed author transports and their tests no longer ship;
  the replacement Bob factory and refusal tests pass. This is not a claim of live Bob
  model qualification.

- The screenshot helper now restores/foregrounds and redraws its own test window before capture. An incomplete background capture was corrected and the Bob window was visually checked.


## Observed 1.1.4 timer checks

- Added a monotonic elapsed clock beside GO, independent of progress messages; it
  includes cancellation cleanup, freezes on completion/error, and resets on the next run.
- `python -m pytest -q tests/gui -m "not ltspice" --tb=short`: 14 passed,
  1 real-simulator integration test deselected. New cases drive actual GO clicks and a
  quiet worker through success, blocked results, exceptions, cancellation and restart.
- `python -m ruff check .` and `python -m ruff format --check .`: passed.
- `installer/build.ps1 -Version 1.1.4`: succeeded, including the actual frozen GUI launch.
- Root Install.exe matches the release ZIP and canonical setup; checksum verified.
  The pepper animation retains 90 frames. No paid API requests were made.
- Agent roles and present LM358 qualification limits are documented in AGENT_WORKFLOW.md.


## 1.1.5 LM358 correction — 2026-09-20

Added the exact-datasheet reviewed profile, dual-op-amp DC/AC/transient probes, complex raw support, and op-amp follower export. Offline suites initially found a missing probe question and an outdated registry unit whitelist; both corrected. Final checks and package evidence are recorded below.

Final local checks: `pytest -q -m "not ltspice and not network"`: 1008 passed, 4 skipped (git-ignored vendor originals). `ruff check .` and `ruff format --check .`: passed. Frozen GUI launch and animated package checks: passed; release ZIP and root Install.exe are identical. Shared simulator suite: 151 passed; the subsequently added exported-op-amp-example test also passed. Instrument-only tests use a labeled synthetic fixture, not claimed device data.


## 2026-09-20 — standardized IC symbols (1.1.6)

- `python -m ruff check .` and `python -m ruff format --check .`: passed.
- `python -m pytest -q -m "not ltspice and not network"`: 1014 passed,
  4 skipped (unavailable vendor originals), 154 deselected.
- Shared renderer integration, run in the general checkout:
  `python -m pytest -q tests/reporting/test_symbol_layout.py tests/pipeline/test_make_model.py::test_scenario_a_scripted_template_passes_and_publishes_the_deliverables`:
  8 passed, including actual LTspice schematic-to-netlist pin-order verification and
  a complete simulated publication. The renderer and new test are identical in both editions.
- Opened the saved LM358 model with the new symbol in LTspice 26.0.0.3: visible leads,
  separated labels, all pins within the body height, reference and value outside.
- No paid inference requests were needed for this change. This does not add electrical
  coverage or establish a new live IBM Bob result.


## 2026-09-20 — compound datasheet units (1.1.7)

The saved SN74LVC1GX04 extraction failed before authoring: the unit vocabulary
rejected valid `ns/V` and `°C/W` limits. Recognition now includes time/voltage,
voltage/time, current/time and thermal resistance. Numerator and denominator
prefixes are scaled independently. Frozen characteristics use the same conversion.
Unmeasurable rows remain explicit gaps; accepting a unit does not create a probe.

- `python -m ruff check .` and `python -m ruff format --check .`: passed.
- `python -m pytest -q -m "not ltspice and not network"`: 1028 passed,
  4 skipped (unavailable vendor originals), 154 deselected.
- New checks cover compound scaling, incompatible dimensions, extraction without
  an unnecessary correction request, cached replay and retaining unprobed rows.


## 2026-09-20 — diverse datasheets and bounded recovery (1.1.8)

Implemented independent frozen circuit recipes with physical pin maps, bounded cached
extraction batches, explicit unsupported quantities and coverage gaps, whole-response
deadlines, observed failure feedback on resumed runs, stable candidate retention,
structural library preflight, and corrected RAW/path handling. No provider or reasoning
level is silently substituted. The Bob catalog and API backend remain Bob-only.

Observed checks:
- `.venv/Scripts/python.exe -m pytest -q -m "not ltspice and not network" --disable-warnings`: 1056 passed, 8 skipped.
- Final shared partial-fixture change: `pytest -q tests/authoring/test_circuit_probe.py -m "not ltspice"`: 24 passed, 2 deselected in the general checkout.
- General real-simulator regression: `pytest -q tests/authoring/test_circuit_probe.py tests/authoring/test_live_failure_regressions.py tests/pipeline/test_make_model.py -m ltspice`: 9 passed, 80 deselected.
- Ruff lint/format checks passed before final packaging. The final general frozen GUI opened; the final Bob executable was blocked by Windows signing policy. Earlier Bob startup passes were intermediate builds only.

The live five-datasheet matrix includes TLV9002, SN74LVC1G14, SN74LVC1T45,
TPS7A2033 and LMV331. All five final model files passed 30 independent real-LTspice
functional checks (3, 1, 8, 10 and 8 respectively). Checks cover both amplifier channels,
both logic edges, translator directions, unequal supplies, ground offsets, regulator
full load and shutdown, and comparator output states. The regulator has 14 covered
PASS results and one covered UNKNOWN settling check. All devices retain untested
numeric rows as UNKNOWN. Translator propagation timing was omitted by extraction.

Earlier failed regulator candidates are historical evidence, not the published model.
The final repair request ended with an incomplete provider stream; the last measured
candidate was preserved. Its 731-second run demonstrates that maximum-reasoning API
latency remains unresolved. Latest replay times are not cold-start speed benchmarks.
These tests do not establish every datasheet, all operating conditions, manufacturer
diversity (all five are TI), or a new live Bob account result. Software regression
passes must not be represented as device certification. See DATASHEET_ROBUSTNESS.md.


### Bob installer verification blocked

The final 1.1.8 source build completed, but `installer/verify_gui.py` could not launch
the executable: Windows error 4551, confirmed by Code Integrity events 3077/3033
(signing-policy rejection). An identical launch retry also failed. No code-signing
certificate is installed. No security settings were changed. The tracked root
installer, installation instructions and checksum remain at the previous verified
1.1.7 version; the new Bob source is not yet a verified downloadable binary. Earlier
intermediate 1.1.8 builds opened, but they do not contain the final fixes and are not
being represented as the final build.


Final shared regressions: all 29 circuit-recipe tests passed with real LTspice
(including node aliases and a deliberately wrong output with a DC hint). General
offline suite: 1159 passed, 4 skipped, 159 deselected. Bob focused latest regressions:
29 passed, 4 skipped, 5 deselected. General final frozen executable passed all seven
TLV9002 checks and the release ZIP installer matched the root checksum. General
installer completed with exit 0; installed and built executable SHA256 both equal
51bbecc18c56c8b1be49ba6bf745fccc342a9622d33c07ec25945950da1208ba.

Final source CI at `e85480f` passed: 1058 passed, 9 skipped, 159 deselected; Ruff lint and format passed.
[GitHub Actions evidence](https://github.com/BasamAhmed640/spice-maker-bob/actions/runs/35535579411).

## 2026-09-20 — credential connection check (1.1.9)

SAVE & CHECK KEY now checks the exact newly saved credential asynchronously, with
a 15-second UI deadline, cancellation on close/provider change, no automatic retries,
and fixed messages that never echo provider bodies or exception text. HTTP checks
use a 256-token inference budget and retain model/reasoning settings. Bob checks
use an empty workspace, all documented tool groups disabled, one turn and a 0.05
Bobcoin cap. License acceptance remains a user action. HTTP redirects cannot forward keys.

`pytest -q -m "not ltspice and not network" --disable-warnings`: 1067 passed, 8 skipped, 159 deselected.
Ruff check and format passed. Live general-edition checks accepted the saved valid
credential and rejected an intentionally invalid credential. No saved key was found
in either repository or the inspected Bob JSON/log files.

Both final 1.1.9 frozen GUI startup verifiers passed. The earlier Bob signing-policy
block did not recur on this feature build; no Windows security policy or trust store
was changed. Bob Shell 2.0.4 was installed from its checksum-verified IBM package.
Its optional telemetry was disabled using the documented setting. Live Bob inference
is pending the user's acceptance of IBM's license.

Both packaged 1.1.9 executables passed all seven TLV9002 real-LTspice checks;
release ZIP installer bytes matched the tracked root installers and checksums.
The Bob 1.1.9 installer completed with exit 0, and the installed application opened
successfully with the same executable hash as the tested frozen build.


## 2026-09-20 — minimal local credential storage (1.1.10 source)

- Replaced the credential-vault dependency with current-user DPAPI ciphertext in
  an edition-specific LocalAppData data folder. One selected key is retained.
- Atomic ciphertext-only writes, validation, tampering rejection, environment
  fallback and redacted diagnostics are covered. Settings omit default values.
- `.venv/Scripts/python.exe -m pytest -q -m "not ltspice"`: **1,072 passed, 8 skipped, 159 deselected**.
- After moving data outside installer-owned directories, focused security and
  setup tests: **62 passed** in each edition. Ruff lint and format checks passed.
- Real Windows encryption round trip and a fresh-process read passed. The general
  live key check accepted the encrypted-file credential. Bob's key decrypted, but
  its live check remains blocked by the user's pending IBM license acceptance.
- The final 1.1.10 installer completed with exit 0. The encrypted credential survived
  reinstall outside the installer-managed folder, and the installed frozen application
  reported source=local_file. Its GUI startup check passed and all seven TLV9002
  real-LTspice checks passed. Root Install.exe matches the release ZIP and SHA256SUMS.
- With explicit user approval, three legacy Spice Maker vault credentials were removed
  after fresh-process verification of the encrypted files. Other applications' entries
  were not touched. A scan of release/source files found neither selected saved key.


## 2026-09-20 — selected datasheet isolation and visible progress (1.1.11)

The UCC28251 run reused a folder containing LM358 and oscillator documents. The
model extraction path accidentally selected all three: 93 electrical pages and
18 initial batches. Explicit document selection now limits this run to UCC28251:
45 electrical pages and 9 batches, preserving manufacturing-appendix accounting.
No API calls were made for this before/after planning check.

Progress reports completed/total batches, active requests, retries and elapsed
time every five seconds. Failed work is not counted as completed. Tests cover
reused-folder isolation, cache independence and progress during a blocked request.
Source, package and splash version must match; the window title shows 1.1.11.

General tests: 1,186 passed / 4 skipped. Bob tests: 1,076 passed / 8 skipped.
Ruff passed. Both local frozen GUI launch checks passed. Local Application Control
blocked the packaging tool; a manual GitHub Windows packaging workflow now builds
the same source with no local policy changes and records artifact hashes.

Final 1.1.11 installer built from `9ef259341cf779e1f6a15b919ca44e15c9773690` on GitHub Windows,
installed with exit 0 and verified against executable/installer SHA256 records.
The installed GUI title displays 1.1.11, encrypted keys remained readable, and
all seven TLV9002 real-LTspice checks passed. Root Install.exe and the release ZIP
contain the same installer. The earlier general-desktop update block is resolved
for this tested build; no local Windows security policy was changed.
