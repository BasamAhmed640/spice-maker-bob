# Current status — 2026-09-28 terminal transition

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
