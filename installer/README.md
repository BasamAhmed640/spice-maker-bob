# The Spice Maker installer

`releases\SpiceMaker-win-Setup.exe` is the one-click installer for this application. It
unpacks the frozen app under `%LocalAppData%\SpiceMaker`, adds a Start Menu entry and a
desktop shortcut, and starts it. There are no wizard pages: Velopack's bootstrapper paints
its progress bar over the bottom 12 px of the animated pepper splash while it unpacks.

It installs **only this application**. It never downloads or installs LTspice, Bob Shell,
Python or any other runtime: the LTspice executable is discovered by the app (or pointed at
on its SETUP page) and smoke-tested there, and `doctor` reports plainly when it is missing.

## Startup verification

Version 1.1.1 isolates PyInstaller's PATH to Python and Windows directories. A build
machine's unrelated tools must not supply incompatible DLLs to the application. The
old bundled ICU library lacked the unversioned symbols Qt expected; its original
provenance was not established.

The build now launches the actual frozen GUI from an empty working directory with a
minimal PATH. It requires a visible, responsive Qt model-maker window, saves
`build/gui-startup.png`, and closes that test process. An error dialog or timeout fails
the build before Velopack creates an installer. The Windows download workflow is
configured to call this same script when manually dispatched; that wiring alone is
not evidence that a CI packaging run has occurred. A CLI-only check is insufficient.

## Build it

One-time setup:

```powershell
uv sync --extra packaging            # pyinstaller + pillow + velopack into .venv
dotnet tool install -g vpk --version 1.2.0           # needs the .NET SDK; keep vpk on velopack's version
```

Then, from the repository root:

```powershell
.\installer\build.ps1 -Version 1.1.3
```

The script renders the splash/icon from `render_assets.py`, freezes the app from
`installer/entry.py` through `installer/SpiceMaker.spec` (portable — every path derives
from `SPECPATH`), and hands `dist\SpiceMaker` to `vpk pack`. Output lands in `releases\`:
`SpiceMaker-win-Setup.exe` (and a copy named `Setup.exe`), a full update package, a
portable zip, and the release metadata.

## The frozen entry point

`SpiceMaker.exe` is windowed, so PyInstaller gives it no streams of its own:

|Invocation|What happens|
|---|---|
|`SpiceMaker.exe`|The model-maker window, exactly as `boardmodeler ui`|
|`SpiceMaker.exe --cli <args>`|`boardmodeler.cli.main(<args>)`|
|`SpiceMaker.exe -m boardmodeler.cli <args>`|Same — this is the form `ui/model_maker.py` uses when it re-invokes itself for CHECK ENVIRONMENT, model re-tests and Install into LTspice|

CLI output attaches to the console that started the process, and falls back to
`%LocalAppData%\SpiceMaker\cli.log` (named in the output) when there is none.

## Verify a build

```powershell
.\releases\SpiceMaker-win-Setup.exe --silent        # installs quietly, exit 0
& "$env:LOCALAPPDATA\SpiceMaker\current\SpiceMaker.exe" --cli setup --json
& "$env:LOCALAPPDATA\SpiceMaker\current\SpiceMaker.exe" -m boardmodeler.cli doctor --json
& "$env:LOCALAPPDATA\SpiceMaker\Update.exe" --uninstall --silent
```

The last line is also the documented uninstall (`QuietUninstallString` in
`HKCU\...\Uninstall`), and it removes the install directory and both shortcuts.

## What the freeze drops, and why

The frozen payload only carries what the product imports. `installer/SpiceMaker.spec`
excludes the venv's optional `sim`/`dev` weight (`spicelib` → scipy, matplotlib; `reportlab`
→ PIL), the Qt modules a `QtCore`/`QtGui`/`QtWidgets` app never loads, Qt's software OpenGL
fallback (`opengl32sw.dll`) and its translations, plus the metadata of the excluded
distributions. Without them the installer was 122 MB of Setup.exe over a 254 MB payload;
with them it is ~60 MB over ~124 MB — measured, and the GUI, the CLI and the LTspice
harness were all re-verified after the trim.

One honest consequence: the optional `spicelib` reader is not in the frozen build, so
`doctor` reports `reader_backend: native` with the documented
"spicelib is not installed (optional 'sim' extra); using the native reader" detail. The
native reader is authoritative in any case (D-002).

Nothing here is code-signed (no certificate on the build machine), so Windows SmartScreen
warns on first run.


## Code menu download

The repository tracks the real **Install.exe** at its root, alongside **INSTALL.txt**
and **SHA256SUMS.txt**. GitHub's **Code → Download ZIP** on main includes these files.
After extracting the archive, users run Install.exe directly from the repository folder.
Each edition carries its own verified v1.1.3 setup executable, including the animation.

After a successful build, build.ps1 refreshes all three root files. Commit those files
with a release update to keep the Code menu download current. The installer is stored
as a regular Git binary, so the downloaded archive contains the executable itself.

## Additional release archive

The build also emits `<PackId>-<Version>-Windows-x64.zip`, containing **Install.exe**,
**Read me.txt** and **SHA256SUMS.txt**. Install.exe is the unmodified Velopack setup bundle,
so it retains the animated pepper splash. Python is included in the frozen app. There
is no Python installation step for end users. LTspice and Bob Shell remain separate.
The Bob edition selects package ID SpiceMakerBob and the title Spice Maker Bob from
build_flavor.BOB_ONLY; the general edition uses SpiceMaker. Builds are unsigned.
The Windows download GitHub workflow provides a scripted build entry point; binary reproducibility is not claimed.
