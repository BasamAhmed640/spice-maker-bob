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
