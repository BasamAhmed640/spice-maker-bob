## 2026-09-25 — M1 system-model check-in (Bob edition)

The [general-edition M1 TPS54332DDA side-by-side report](https://github.com/BasamAhmed640/spice-maker/blob/main/docs/evidence/2026-09-25-system-models-m1/REPORT.md)
records the fresh switching-model comparison with TI. Its original broad gain
fit and output-ripple checks are **FAIL**; a separately labeled local gain fit
passes. PH edge realism and full load-step recovery are **UNKNOWN**.
Those are general-edition measurements, not a separate Bob simulation result.
No live Bob model call was made, and the Bob application source was not changed.

Both editions now include `docs/SYSTEM_TEST_SUITE.md`. The owner's supplied
suite text remains a verbatim prefix; its tracker lists 66 unique cases as
`NOT BUILT`, including 12 marked boundary pairs. No board-card fault result is
claimed by this documentation.

Local Bob focused tests, run with `-m 'not network'` and `LTSPICE_EXE` explicitly
set to `C:\Users\basam\AppData\Local\Programs\ADI\LTspice\LTspice.exe`,
reported **594 passed, 7 skipped in 168.30s**. `ruff check src tests tools`
passed. The shared-core comparison found **42 identical files** in the two
editions. M2 is the next milestone after M1 evidence is finalized.

# 1.4.0 portable Bob safety update — 2026-09-24

The Bob edition now uses a project-local Python `.venv` with pinned runtime
`requirements.txt` and separate development requirements. Its root `AGENTS.md`,
`.bob/rules/`, and `.bobignore` follow IBM Bob's documented project layout. Bob
author turns run with `--workspace` set to a generated scratch folder and all
documented tool groups disabled. Bob replies with library text; the application
validates and writes the model. Legacy tool-enabled Bob Shell extraction and the
undocumented Bob Direct HTTP extraction path are disabled, as is the old HTTP
inference selection in this edition. IBM Bob project rules apply when editing the
repository in Bob IDE; the embedded author uses app-supplied prompt rules instead.

LTspice must be chosen and saved explicitly. An inherited `LTSPICE_EXE` and
well-known install paths are never adopted. Simulator children receive a scrubbed
environment without inference keys, retaining existing LTspice user profile
variables needed for batch execution and using an app-local temp directory.
GUI full electrical verification is the default; explicit quick mode labels its
output electrically unverified. A PASS still requires a measured LTspice artifact.

Local observed checks on Windows, Python 3.14.2: `uv sync --frozen --all-extras`
and installation of `requirements.txt` into `.venv` passed; `ruff check src tests`,
`compileall`, and `git diff --check` passed. The simulator-free suite reported
**1206 passed, 14 skipped, 163 deselected**. A local doctor check without an
LTspice selection reported not configured. After the path was explicitly saved,
real LTspice batch smoke completed in 0.54 seconds with `.raw` and `.log` hashes
and measured `V(out)=0.632119 V` for the fixture circuit. This is a simulator
smoke check, not a generated IC model accuracy claim. `bob run --help` confirmed
the tool-disabling CLI flags. No Bob API key was available, so live Bob authoring
and a fresh generated model were **not tested**.

The portable installer was rebuilt locally with the tracked `Install.exe` for
GitHub Code → Download ZIP. Frozen GUI startup passed, and portable verification
passed in two separate extracted copies, each with a separate bundled Python
`.venv` and local data. The installer SHA-256 is
`0696da81daa23146e6d4c778e1199ae7731ea113c1cc1a3d698f62918fa773b8`.
See `docs/BUILD_VERIFICATION.json` for exact artifact checks. The PowerShell build wrapper was blocked by the local
execution policy; its reviewed Python steps were invoked directly without
changing the policy. No OS security feature was disabled.

Post-install correction: the first portable package contained an older bundled
application wheel even though its frozen GUI had current source. Consequently,
the installed `Boardmodeler.cmd` redirected LTspice's profile and its doctor
smoke stalled. The wheel was rebuilt, its `storage.py` was verified byte-for-byte
against source, and packaging now refuses a stale wheel. In a newly installed
copy with LTspice explicitly set, `Boardmodeler.cmd doctor --json` reported
LTspice 26.0.0, `searched=false`, smoke PASS in 0.63 seconds, recorded `.raw`/`.log`
hashes and measured `V(out)=0.632119 V`. Bob authoring still awaits a key.

## Prior status

Installer run 35572967282 and source CI 35572967584 passed for `8abf88658c4effdd25fa31b96ae9701703730ba6`.
The actual installer passed GUI startup, first-launch setup, same-folder data preservation
and fresh-copy isolation on GitHub Windows. That verified installer and its window/splash
strings are 1.3.0; the source tree is now 1.4.0 (see the entry below) and a fresh installer
verification must pass on it before publication.
Install.exe is tracked directly for Code > Download ZIP. See BUILD_VERIFICATION.json
for source provenance and installer hash. No live provider or broad device-accuracy
claim is established by these checks.

## 2026-09-21 — 1.4.0: resizable windows, the whole doctor report, user-driven LTspice search

Hand-adapted from the main edition, keeping this edition Bob-only (one catalog entry, no
provider row, `BOB API KEY` label):

- The main window and SETUP are resizable again (`resize(900, 600)` plus a content-derived
  floor, no `setFixedSize`); SETUP keeps every row reachable through a scroll area.
- CHECK ENVIRONMENT opens `DoctorView` with the entire report (readable first, raw JSON one
  click away, COPY REPORT) instead of a message box holding only the last 4000 characters.
- `HourglassWidget` draws itself next to the elapsed clock, animates only while a build runs
  (80 ms timer, stopped when idle) and ships no binary asset.
- LTspice is never searched on open: SETUP has explicit FIND and BROWSE buttons, and the
  status line distinguishes not-set-yet, found-by-that-search, browsed and saved-configuration.
- Inference egress is catalog-bound: `agent_providers.endpoint_is_vendor` compares the whole
  host, and `http_inference.require_vendor_endpoint` refuses before any header, credential
  lookup or socket. Bob is a CLI entry with no HTTP endpoint, so naming it refuses every URL
  rather than inventing one.
- The credential file is plain local JSON (`data/credentials.bob.json`), not DPAPI ciphertext;
  user-facing text, `INSTALL.txt`, `README.md` and the docs now say so.
- Version: `pyproject.toml` and `boardmodeler.__version__` are 1.4.0. `installer/verify_portable.py`
  is a shared file and still asserts the 1.3.0 window title, so a 1.4.0 installer build cannot
  pass it until that shared assertion is updated in the main tree.

Not yet verified here: no installer run was performed for this entry;
`installer/verify_portable.py` has not been executed.

Verification of this source state (all run in the Bob checkout with `uv run`):

- `uv sync --all-extras` — resolved, 40 packages checked.
- `ruff check src/ tests/` — `All checks passed!`
- `pytest -q -m "not ltspice"` — **1209 passed, 13 skipped, 163 deselected**. The 5 new
  skips are edition scope, each with its reason in the report: two
  `tests/test_portable_storage.py` assertions that name the main edition's
  `data/credentials.json`, and three `tests/authoring/test_reinforce.py` assertions that
  name the main catalog's `api-docs.deepseek.com`. `tests/authoring/test_key_http.py`
  (HTTP key verification for providers this edition does not ship, importing
  `verify_http_key` from the main-only `api_backend.py`) is not collected. All three
  scopes live in the edition-owned root `conftest.py` and are conditional on the edition
  data that makes them true.
- `doctor --json` — `"version": "1.4.0"`; credentials section names Bob only
  (`"bob": "credential 'bob_shell': source=missing"`); `setup --json` reports
  `accepted_providers: ["bob"]`.
- `pytest -q tests/test_desktop_retry.py tests/ui/ tests/gui/` — **87 passed**.
- Main tree, `uv run python tools/sync_shared_core.py ../spice-maker-bob` —
  `0 shared files differ`.

**Known shared-file staleness (cannot be fixed from this checkout):**
`installer/verify_portable.py:239,259,262` still writes a `data/credentials.bin` sentinel
and asserts the 1.3.0 window title, and `installer/README.md:3` still builds
`-Version 1.3.0`. Both files are shared, so a 1.4.0 installer verification is red until
those literals are corrected in the main tree and re-synced.

## 2026-09-21 — 1.3.0 source ready for installer verification

Fixed behavioral-source repair scope (including nested subcircuits, forward current
references and local name collisions), cancellation during cached load checks and
contradictory quick-mode documentation. Old sanity receipts are invalidated.
The Windows download workflow now defaults to the checked-out source version.

Real LTspice exposed a successful operating-point run being labelled inconclusive
after watchdog cleanup. A completed log plus readable finite operating-point data
now establishes `loaded` even when the watchdog terminates the finished process.
Missing/truncated data and missing completion markers remain inconclusive. This is
not electrical accuracy verification and never changes electrical rows to PASS.

Both editions: `python -m pytest -q tests/authoring/test_sanity.py
tests/authoring/test_model_syntax.py tests/pipeline/test_quick_mode_rejection.py`:
45 passed each, including two real LTspice cases (rejection prevents export;
independent subcircuits load after repair). Earlier focused authoring/publish/GUI
regressions passed 66 per edition before the three output-completion cases were added.
`ruff check .`, `ruff format --check .`, and `git diff --check` passed.
No inference requests were made. Full source CI and fresh installer verification
must pass on this source before publication. No five-device accuracy claim is made.

# Quick structural-check mode 1.3.0 — 2026-09-21

GUI GO now defaults to local structural checks, with no AI test-fixture planning or
LTspice simulation. Full verification is an explicit setup choice or follow-up
button. Quick exports retain UNKNOWN electrical status and say accuracy is unverified.
There are at most two authoring turns, hash/spec checked reuse, and no published stale
candidate after an empty response. All app storage remains folder-local.

Validation: 11 structural/pipeline cases and two GUI/persistence cases passed in the
source suite. The general focused pipeline/GUI run passed 77 tests. The first full
suite exposed old mock Request signatures that lacked the new verification field;
after updating those fixtures, the affected CLI/provider suite passed all 18 tests
in each edition. Ruff passed. Final source CI and actual packaged startup verification
are required before the installer is added to main.

The actual UCC28251 model from the ongoing 1.1.11 run passed the local static check.
That is not an observed electrical-accuracy result and required no new inference.

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

## 2026-09-24 — convergence-first authoring and the shared core (Bob edition)

Branch `convergence-shared-core`. The 41 extraction/authoring/verification core modules are
byte-identical to Spice Maker's (`shared_core.json`, `tests/test_shared_core.py`); `.bob/`
rules, `.bobignore` and the tool-free Bob Shell are unchanged. The generated-model evidence
(TPS54332DDA, LM358) was produced with the general edition's HTTPS provider, because this
machine has no Bob API key. It is recorded in Spice Maker's
`docs/evidence/2026-09-24/REPORT.md`.

| Check | Result |
|---|---|
| `pytest -q -m "not gui and not network"` with `LTSPICE_EXE` set | 1215 passed, 7 failed. The same 7 fail at the untouched parent `aebf457` (`tests/test_cli_doctor.py` x2, `tests/test_cli_run_tests.py` x5), so they predate this change |
| `pytest -q tests/authoring tests/test_shared_core.py` with LTspice configured | 389 passed, 7 skipped |
| LM358 build with no Bob key | BLOCKED `bob_credentials_unavailable`; no fallback to the OpenCode/DeepSeek keys present in the environment. It used to crash while writing deliverables; fixed and tested |
| LM358 build with no LTspice selected | BLOCKED "LTspice is not configured; choose its executable in SETUP" |
| Fresh GitHub ZIP of the branch: venv, pinned install, startup without LTspice access, explicit `.op`, credential scan | PASS (7/7 steps) |

## 2026-09-24 — 1.5.0 (Bob edition): readiness lights, shared-core fixes, stale tests removed

Shared core identical to Spice Maker 1.6.0 (41 files): pdfium fallback for PDF reads, gate-then-
parallel harness, recovered operating points counted as found, A-device node-count lint. The
build window shows API KEY / BOB SHELL / LTSPICE / PDF / OCR / INTERNET lights and VERIFY KEY &
TOOLS: BOB SHELL is red when `bob` is missing or `bob run --help` lacks a flag builds pass, then
Bob Shell answers once with every tool group disabled. Symbols follow the four-sided convention.
Evidence: Spice Maker's `docs/evidence/2026-09-24-usability/REPORT.md`.

| Check | Result |
|---|---|
| Bob Shell on this machine | 2.0.4 at `pi-node\current\bob`; all 7 build flags listed; no Bob key, so VERIFY stops at API KEY |
| Full suite incl. GUI (`e103f49`) | 1448 passed, 25 skipped, 4 failed; the 4 were stale tests (doctor expected `LTSPICE_EXE` discovery; an integration test expected PASS with 21 unverified fixture rows) and are fixed in `381f65f`/`8d2d909` |
| `tests/test_cli_doctor.py tests/test_cli_run_tests.py` after the fix | 17 passed |
| `tests/pipeline/test_make_model.py` after the fix | 55 passed |

## 2026-09-25 — template-first buck release (Bob edition, 1.6.0)

Decisions D-041–D-043 cover the shared deterministic seed and physical fixture
checks. The general edition's detailed TPS54332DDA and TPS54331 measurements
are recorded in its `docs/evidence/2026-09-25-buck-template/REPORT.md` and
`docs/evidence/2026-09-25-tps54331-second-device/REPORT.md`. Those measurements
are evidence for the shared template and harness, not a Bob-agent authoring run.

| Check | Observed result |
|---|---|
| Offline Bob product build, `--backend bob --no-reinforce --iterations 1 --json` | Exit 0 in 146.1 s; published `.lib` and model card under `C:\Users\basam\src\.smsnap\r2\models\B1-template-product`; status UNKNOWN, 12 PASS / 4 FAIL / 3 UNKNOWN |
| Shared TPS54332DDA seed and general-edition LTspice measurement | 12 PASS / 4 FAIL / 3 UNKNOWN on 19 frozen rows; known bad fixture designs were not rewritten |
| Shared TPS54331 seed and general-edition LTspice measurement | 3 PASS / 0 FAIL / 0 UNKNOWN on three cited rows; current limit not measured |
| Focused Bob security/switch tests | 5 passed |
| Release/source credential scan | 0 findings in 6,013 files |

The Bob product build used `BOARDMODELER_NO_NETWORK=1`, the official local
TPS54332 PDF, frozen local requirements and bindings, and an explicitly selected
LTspice executable. Bob repair refused before making a request; no Bob key or
live Bob inference was used. The product's model verdict remains UNKNOWN.

Subsequent release checks on this machine:

| Check | Observed result |
|---|---|
| Full suite | 1,486 passed / 25 skipped / 0 failed in 364.12 s |
| Direct GUI control sweep | 5 surfaces, 57 controls, 38 clicked, 0 errors |
| General-edition LM358 re-verification of the shared harness | 19 PASS / 0 FAIL / 0 UNKNOWN in 13.623 s; no Bob provider run |
| Shared core comparison with general edition | 41 files byte-identical |
| `ruff check .` | Clean; `ruff format --check .` still has older unrelated formatting drift |
| Final local v1.6.0 `Install.exe` | PASS: GUI startup, install, update, two isolated copies, no outside-folder additions; 89,463,296 bytes; SHA256 `0b44f3b9976b66821f7654bdc066791dd69ee12ce5e4ce90a205b05b4509921b` |
| Final release/source credential scan | 0 findings in 8,483 files |

The general edition's [full TPS54332DDA GUI build](https://github.com/BasamAhmed640/spice-maker/blob/main/docs/evidence/2026-09-25-gui-build/REPORT.md)
delivered a convergent but UNKNOWN model in 1,230.314 s (20m30s); it was
not a Bob inference run. No Bob API key is available here for a live Bob turn.
Final local installer checks above include the terminal-stage UI fix and
fail-closed Internet setting. Main commit `68ee75d8b4bcabe72053fb409cfd4a1899584bf4` was
pushed to GitHub; a fresh Download ZIP from that commit passed archive,
extraction, installation, CPython 3.14.2 environment setup, explicit LTspice
path selection, model build, and saved-model retest. The frozen GUI opened.
The fixture build judged 9 PASS / 0 FAIL / 21 UNKNOWN / 8 NOT_APPLICABLE;
the saved model retest passed all 8 LTspice checks. Bob's CLI does not have a
`model open` command in 1.6.0, but `model test --out` reloads a saved model
and reruns its verification. See [fresh-download evidence](evidence/2026-09-25-release/REPORT.md).

## 2026-09-25 — buck template slice (shared with Spice Maker)

Same shared-core and template change as Spice Maker (D-045–D-047): gates on the model's own
ground, benches that must touch node 0, buck rules through sense elements and pin aliases,
statement-based parameter mapping, deterministic current-limit bench. Evidence is in Spice
Maker's `docs/evidence/2026-09-25-buck-slice/REPORT.md`; next families in `docs/TEMPLATE_CATALOG.md`.

| Check | Result |
|---|---|
| `pytest tests/authoring tests/models tests/pipeline tests/ltspice tests/test_shared_core.py` (`LTSPICE_EXE`) | 594 passed, 7 skipped (HTTP-only / general-catalog tests), 0 failed |
| `tools/shared_core.py --compare ..\spice-maker` | identical: 42 files |

## 2026-09-25 — system-level models M0: shared direction and catalog (Bob edition)

M0 changes documentation only. Bob mirrors D-048, the 22-family
[`TEMPLATE_CATALOG.md`](TEMPLATE_CATALOG.md), and the M0–M9+
[`SYSTEM_MODELS_PLAN.md`](SYSTEM_MODELS_PLAN.md). Only the existing
`peak_current_buck_v1` slice has reusable family behavior today; it is not
system-verified. Bob Shell remains tool-free with no general-provider fallback.
M1 is next, beginning in the general edition.

| Check | Observed result |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q tests\test_shared_core.py` | 2 passed in 1.12 s |
| General `.venv\Scripts\python.exe tools\shared_core.py --compare ..\spice-maker-bob` | identical: 42 files |
| D-048, catalog and plan comparison with general | Decision text equal; catalog and plan SHA-256 hashes equal |
| `git diff --check` | No whitespace errors |

The [M0 evidence report](https://github.com/BasamAhmed640/spice-maker/blob/main/docs/evidence/2026-09-25-system-models-m0/REPORT.md)
is in the general repository. M0 did not run LTspice or a Bob inference turn;
no new Bob electrical result is claimed. Older FAIL and UNKNOWN results remain.
