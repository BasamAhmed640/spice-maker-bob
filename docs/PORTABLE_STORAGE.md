# Portable storage

The folder extracted from GitHub is the storage boundary for this copy:

- `Install.exe`: portable installer with a progress bar and timer, included in the release ZIP.
- `app/`: the frozen application and the Python it is bundled with.
- `env/python/`: the vendored CPython runtime; no Python is required from outside this folder.
- `env/wheels/`, `env/requirements.txt`, `env/wheels.sha256`: the pinned wheel set and its checksums.
- `.venv/`: this copy's own environment; stale packages upgrade from checked local wheels.
- `Start.cmd`, `Boardmodeler.cmd`, `Spice Maker.lnk`: folder-local launchers for the app and the command line.
- `.setup.log`: observed steps and exit codes of the last setup; `.venv-setup-error.txt` appears only when the environment could not be created.
- `data/config.json`: first-launch settings, selected executable path and relative model folder.
- `data/credentials.json`: one API key as an ordinary local JSON file (Bob edition:
  `credentials.bob.json`).
- `data/temp/`, `data/logs/`, `data/plot-cache/`: scratch work and diagnostics.
- `data/bob-profile/`: isolated profile for IBM Bob Shell when used by this copy.
- `models/`: default model output, including datasheet copies, evidence, caches and simulator runs.
- `library/`: model/symbol export inside this folder; add `library/sym` and `library/sub` to LTspice search paths manually if needed.

SETUP is required on first launch. Choose the existing LTspice executable and a model
folder under this extracted folder. The executable is read from its existing location;
the app does not relocate or install LTspice. The chosen path is saved in
`data/config.json`. The app does not search for an installation; use SETUP's BROWSE
button to choose the executable yourself. An inherited `LTSPICE_EXE` value is ignored.
Changing working directory cannot change
where this copy stores data. Model paths are saved relative to the portable root, so
moving the entire folder on the same Windows account preserves that preference.

Setup builds `.venv` from `env/` alone: pip runs with `--no-index --find-links env/wheels`
after every wheel has been checked against `env/wheels.sha256`, so no package index is
contacted and no wheel is installed unchecked. A caller's `PYTHONHOME`, `PYTHONPATH` or
`VIRTUAL_ENV` is stripped from the child environment first. A matching engine stays unchanged;
a stale version or source fingerprint upgrades packages in staging and validates before activation,
with rollback on failure. The interpreter, configuration and user data are preserved.
The wheel set carries no Qt: the GUI runs from the frozen `app/`, commands that
open a window (`ui`, `setup`) are served by `app\SpiceMaker.exe --cli ...`, and `.venv`
serves the script and command line surface that does not need a window.

Several copies can live side by side. Each one derives its root from its own executable, so
`data/`, `models/`, `env/` and `.venv/` belong to that copy alone: `.venv/pyvenv.cfg` names
that copy's `env/python` as its base, and nothing written by one copy names another.
Installing, updating or deleting one copy therefore leaves the others untouched, including
when two copies are installed at the same time, because the install lock is inside each root.
The edition marker only stops the two editions from sharing a single folder.

Windows stores an absolute target inside a shortcut, so moving the whole folder leaves
`Spice Maker.lnk` pointing at the old path. `Start.cmd` and `Boardmodeler.cmd` resolve their
own folder (`%~dp0`), so they keep working after a move; re-running `Install.exe` in the
moved folder refreshes the shortcut.

No AppData settings or credential files are read or written. No Credential Manager
entries, installer registration, desktop/Start Menu shortcuts, global updater, or
telemetry are created. There is no automatic import of an older installation's key.
Delete the entire extracted folder for a fresh start. Replacing only Install.exe or
running it again inside an existing folder updates app/ and env/ and preserves data/,
models/ and the .venv interpreter, while bringing stale packages to the delivered engine.
Keep the editions in separate folders; the installer rejects mixing
them in one folder. An older AppData-era install (for example a Velopack `Update.exe` entry
under `HKCU\...\Uninstall`) is not read, changed or removed by this installer.

The key remains sensitive. It is stored unencrypted in this folder's data directory and
is protected only by the folder itself, so keep the copy private and do not share its
`data` directory. There is no Windows-held key: no DPAPI ciphertext, no registry entry
and no Credential Manager entry, and copying the whole folder to another account or PC
carries the key with it. To forget it, delete the credential file and enter the key again.

Spice Maker restricts its Python file writes to this folder and gives child temporary
work and the Bob profile local paths. This is application storage containment, not an
OS sandbox: Windows, antivirus and external tools such as LTspice may maintain their
own system records. API use sends selected content to the configured service.

The UCC28251 replay in this change returned usable responses after 95.0 and 132.4
seconds. The latter used flat evidence.pdf_page instead of evidence.page.pdf_page.
That exact unambiguous shape is now normalized locally; conflicts, invalid values,
unknown units, numerical limits and provenance checks remain unchanged. This avoids
an unnecessary repair call for that recorded response. It does not prove that every
UCC28251 behavior has been modeled or that the full run has completed.
