# Animated portable installer

Run `uv sync --frozen --all-extras`, then `installer/build.ps1 -Version 1.2.0`.
The build freezes the GUI, launches it to verify startup, then compiles
`PortableInstaller.cs` using the .NET Framework compiler included with Windows.
The executable embeds the application ZIP and the existing animated pepper GIF.
No updater service, AppData installation or registry registration is created.

Install.exe extracts into app/ beside itself and writes Start.cmd. Staging and rollback
folders also stay beside it. Existing data/ and models/ are preserved on update.
An edition marker prevents installing the other edition into the same root.
`Install.exe --silent --no-launch` supports verification without opening the app.
Build-time GUI test data is excluded from the installer payload.

The root Install.exe is committed intentionally so GitHub Code > Download ZIP contains
the real installer. Releases also provide the same installer and a convenience ZIP.
See ../docs/PORTABLE_STORAGE.md for storage, first-launch setup and fresh-start behavior.
