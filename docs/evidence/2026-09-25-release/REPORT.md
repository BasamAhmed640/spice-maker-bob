# Bob 1.6.0 fresh-download release check

On 2026-09-25, the Windows check downloaded the `main` ZIP from
`https://codeload.github.com/BasamAhmed640/spice-maker-bob/zip/refs/heads/main`
after main commit `68ee75d8b4bcabe72053fb409cfd4a1899584bf4` was pushed.
The downloaded ZIP was 90,893,437 bytes, SHA256
`f2db4a58749fb0504240200b8b0eeebde1a87b9c89a6e6fc12ee5646d679ed32`.
Its root `Install.exe` was 89,463,296 bytes, SHA256
`0b44f3b9976b66821f7654bdc066791dd69ee12ce5e4ce90a205b05b4509921b`,
matching `SHA256SUMS.txt`. Windows file metadata and `pyproject.toml` both
identified version 1.6.0.

| Stage | Result | Observed |
|---|---|---|
| Download and inspect archive | PASS | 403 members; expected installer, instructions, README and hash file present |
| Extract | PASS | 333 files extracted; no rejected member or file outside the destination |
| `Install.exe --silent --no-launch` | PASS | Exit 0 in 24.2 s; app, local `.venv`, bundled Python, launchers and setup log present; no created or changed AppData entry and no added item beside the extracted root |
| Environment | PASS | Bundled CPython 3.14.2 and `boardmodeler` 1.6.0; packages installed from the pinned offline wheels; configuration and Bob credential paths scoped to this extracted copy |
| LTspice selection | PASS | Explicitly saved `LTspice.exe` path used by the frozen app; there is no automatic search or inherited path override |
| Model build | PASS as a pipeline run; model verdict **UNKNOWN** | Offline synthetic TPS54320 fixture: 9 PASS, 0 FAIL, 21 UNKNOWN, 8 NOT_APPLICABLE requirement rows. `.lib`, symbol, card and results were saved |
| Saved model retest | PASS | `model test --out` reloaded that saved model and reran LTspice checks: 8 PASS, 0 FAIL, 0 UNKNOWN |
| Installed frozen GUI | PASS | A responsive **Spice Maker setup** window opened in 3.73 s from the downloaded and installed `app/SpiceMaker.exe`. [Screenshot](github-installed-setup.png) |
| `model open` CLI | SKIPPED | Version 1.6.0 has no such command; the saved model can be reverified with `model test --out` and the GUI offers **Open model folder** and **Run tests again** |

The model fixture uses a stand-in PDF and test requirements. Its eight passing
simulator checks demonstrate the install-to-retest journey; they do **not**
qualify the TPS54320 as a complete device model. The 21 UNKNOWN rows are
unverified. No Bob API key was available, and this run made no live Bob
inference. The setup screenshot shows no credential value.

The release checker intentionally stopped after the model-open capability
probe, so ZIP regeneration and cleanup stages were not part of this fresh-
download run. The installer and root hash file had been built and checked in
the local release workflow before the push.
