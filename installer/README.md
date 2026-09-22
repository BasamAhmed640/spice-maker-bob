# Animated portable installer

Run `uv sync --frozen --all-extras`, then `installer/build.ps1 -Version 1.3.0`.
The build vendors the in-folder Python environment (`vendor_env.py`, the only step that
uses a package index), freezes the GUI, launches it to verify startup, then compiles
`PortableInstaller.cs` using the .NET Framework compiler included with Windows.
The executable embeds three payloads: the frozen application, the vendored environment
and the animated pepper GIF. No updater service, AppData installation or registry
registration is created.

## What setup writes, and where

Everything is inside the folder that holds `Install.exe`:

| Path | What it is |
| --- | --- |
| `app/` | the frozen GUI and its own bundled Python (replaced on update) |
| `env/python/` | the vendored CPython runtime, so the target machine needs no Python |
| `env/wheels/` | the pinned wheel set and `wheels.sha256`, verified before installation |
| `env/requirements.txt` | the exact pins pip installs, including `boardmodeler` itself |
| `.venv/` | this copy's own environment, created once from `env/` and then kept |
| `Start.cmd` | starts this copy's app; uses `%~dp0`, so it survives a folder move |
| `Boardmodeler.cmd` | runs this copy's command line from `.venv` |
| `Spice Maker.lnk` | folder-local shortcut to `Start.cmd` |
| `.setup.log` | observed steps, exit codes and child output of the last run |
| `.venv-setup-error.txt` | written only if the environment could not be created |
| `data/`, `models/`, `library/` | the app's own storage, untouched by setup |

Staging and rollback folders also stay beside it. Existing `data/`, `models/` and `.venv/`
are preserved on update. An edition marker prevents installing the other edition into the
same root. `Install.exe --silent --no-launch` supports verification without opening the app.

## The environment: offline, and why it has no Qt

`.venv` is a real CPython 3.14 environment built at install time by `env/python/python.exe
-m venv`, then filled by pip with `--no-index --find-links env/wheels`, so it works with no
network at all. Nothing outside the folder is read or written, and a caller's `PYTHONHOME`,
`PYTHONPATH` or `VIRTUAL_ENV` is stripped from the child environment first. The wheel list
is verified against `env/wheels.sha256` before anything is installed; a mismatch or a
missing file fails the step instead of installing unchecked wheels.

`PySide6-Essentials` is deliberately **not** in that wheel set: it is ~77 MB compressed, and
the installer is committed to the repository as a single file whose size must stay under
GitHub's 100 MB per-file limit. The GUI therefore runs from the frozen `app/`, exactly as
before, and the Qt-based subcommands (`ui`, `setup`) are served by
`app\SpiceMaker.exe --cli <command>`. The `.venv` serves the script and command line surface
that does not need Qt (`version`, `doctor --json`, `model ...`). If the environment cannot be
built, setup still installs a working app, writes `.venv-setup-error.txt` and says so.

Cost of this choice, measured on the build machine: `Install.exe` grows by ~35 MB
(89 MB total), and each copy uses ~150 MB more disk for `env/` and `.venv/`.

## The shortcut, and moving the folder

Windows stores an absolute target in a `.lnk`, so a relative target cannot be written
(`WScript.Shell` resolves it at save time; observed). The shortcut therefore points at
`<this folder>\Start.cmd`. After moving the folder, double-click `Start.cmd`: it resolves
its own location with `%~dp0` and keeps working. Re-running `Install.exe` in the moved folder
refreshes the `.lnk`. No Start Menu or desktop entry is ever created.

## Copies do not interfere

Each copy derives its root from its own executable: `app_root()` is the folder holding
`app/`, `data/` is `<root>/data`, and `.venv/pyvenv.cfg` names `<root>/env/python` as its
base. Two copies of the same edition in different folders are independent — including when
they are installed at the same time, since the install lock is inside each root. The
edition marker only stops the two *editions* from sharing one folder.

## Verification

```powershell
python installer/vendor_env.py        # vendor the runtime and wheel set (needs a package index)
python installer/package_portable.py  # compile Install.exe from the payloads
python installer/verify_portable.py   # install/update/two-copy/fresh-copy verification
```

`verify_portable.py` installs two copies concurrently, checks each one's interpreter, base
prefix, configuration path, launchers and shortcut, asserts the first copy is byte-identical
after the second is installed, and asserts that no AppData, Start Menu, desktop or registry
entry was created.

**Smart App Control:** on a machine with Smart App Control enabled, a *freshly built*
unsigned `Install.exe` is blocked when launched (`WinError 4551`, CodeIntegrity events
3089/3077/3033) until the binary has reputation or is signed. The previously shipped binary
still runs. That blocks `verify_portable.py` (and therefore `build.ps1`) on such a machine
for a new build; the installer logic itself can still be exercised in-process from the same
source (see `installer/README.md`, "Verification"), and signing the release is the real fix.

The root `Install.exe` is committed intentionally so GitHub Code > Download ZIP contains
the real installer. Releases also provide the same installer and a convenience ZIP.
See ../docs/PORTABLE_STORAGE.md for storage, first-launch setup and fresh-start behavior.
