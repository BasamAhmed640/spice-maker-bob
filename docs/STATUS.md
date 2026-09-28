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
- Post-change full test, format, source ZIP, shortcut, no-Python, offline, long-path, Bob Shell, and ARM64 results are pending. Their exact commands and output belong in [`docs/evidence/2026-09-28-terminal/REPORT.md`](evidence/2026-09-28-terminal/REPORT.md) once observed.

No live Bob authoring call has been made for this change.
