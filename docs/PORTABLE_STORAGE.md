# Portable storage in 1.4.0

The folder extracted from GitHub is the storage boundary for this copy:

- `Install.exe`: animated installer, included in Code > Download ZIP on main.
- `Start.cmd` and `app/`: launch command and bundled application/Python.
- `data/config.json`: first-launch settings, selected executable path and relative model folder.
- `data/credentials.bob.json`: one API key as an ordinary local JSON file, **not encrypted**.
- `data/temp/`, `data/logs/`, `data/plot-cache/`: scratch work and diagnostics.
- `data/bob-profile/`: isolated profile for IBM Bob Shell when used by this copy.
- `models/`: default model output, including datasheet copies, evidence, caches and simulator runs.
- `library/`: model/symbol export inside this folder; add `library/sym` and `library/sub` to LTspice search paths manually if needed.

SETUP is required on first launch. Choose the existing LTspice executable and a model
folder under this extracted folder. The executable is read from its existing location;
the app does not relocate or install LTspice. Changing working directory cannot change
where this copy stores data. Model paths are saved relative to the portable root, so
moving the entire folder on the same Windows account preserves that preference.

No AppData settings or credential files are read or written. No Credential Manager
entries, installer registration, desktop/Start Menu shortcuts, global updater, or
telemetry are created. There is no automatic import of an older installation's key.
Delete the entire extracted folder for a fresh start. Replacing only Install.exe or
running it again inside an existing folder updates app/ and preserves data/ and models/.
Keep the editions in separate folders; the installer rejects mixing them in one folder.

The key remains sensitive. The file is not encrypted and is not tied to a Windows account:
anyone who can read this folder can read the key, so keep the copy private. Generated data
and keys
are excluded from Git and from packaging. Sharing the entire folder manually would
still share its private model data and its key file; share a clean GitHub download.

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
