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
