"""Compile the animated, folder-local installer using Windows .NET Framework.

Two payloads are embedded, because the installer keeps them in two folders:

* ``payload.zip`` - the frozen application, unpacked into ``app/``;
* ``env.zip`` - the vendored CPython runtime and wheel set, unpacked into ``env/``
  and used once to create ``.venv`` beside them (``installer/vendor_env.py``).

``version.txt`` carries the release version into the splash title and ``AssemblyInfo.cs``
is generated from the same value, so nothing in this build hard-codes a version.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
import zipfile
from pathlib import Path


def _zip_tree(source: Path, target: Path, skip: set[str] | None = None) -> Path:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(source).as_posix()
            if skip and relative.split("/", 1)[0] in skip:
                continue
            archive.write(file, relative)
    return target


def _assembly_info(version: str, target: Path) -> Path:
    target.write_text(
        f'[assembly: System.Reflection.AssemblyVersion("{version}.0")]\n'
        f'[assembly: System.Reflection.AssemblyFileVersion("{version}.0")]\n'
        f'[assembly: System.Reflection.AssemblyInformationalVersion("{version}")]\n',
        encoding="ascii",
    )
    return target


def package(repo: Path, edition: str) -> Path:
    build = repo / "build" / "portable"
    build.mkdir(parents=True, exist_ok=True)
    version = str(
        tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    )
    wheels = list((build / "env" / "wheels").glob(f"boardmodeler-{version}-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("application wheel missing; run installer/vendor_env.py before packaging")
    with zipfile.ZipFile(wheels[0]) as application_wheel:
        for source in (repo / "src" / "boardmodeler").rglob("*.py"):
            member = source.relative_to(repo / "src").as_posix()
            try:
                packaged = application_wheel.read(member)
            except KeyError as error:
                raise RuntimeError(f"application wheel lacks {member}; rebuild the environment") from error
            if packaged != source.read_bytes():
                raise RuntimeError(f"application wheel is stale at {member}; rerun installer/vendor_env.py")
    payload = _zip_tree(
        repo / "dist" / "SpiceMaker", build / "payload.zip", {"data", "models", "library"}
    )
    environment = _zip_tree(build / "env", build / "env.zip")
    (build / "edition.txt").write_text(edition, encoding="ascii")
    (build / "version.txt").write_text(version, encoding="ascii")
    assembly = _assembly_info(version, build / "AssemblyInfo.cs")
    framework = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    target = repo / "Install.exe"
    subprocess.run(
        [
            str(framework / "csc.exe"),
            "/nologo",
            "/target:winexe",
            "/platform:x64",
            "/optimize+",
            "/out:" + str(target),
            "/win32manifest:" + str(repo / "installer/portable.manifest"),
            "/win32icon:" + str(repo / "installer/assets/pepper.ico"),
            "/reference:System.Windows.Forms.dll",
            "/reference:System.Drawing.dll",
            "/reference:" + str(framework / "System.IO.Compression.dll"),
            "/resource:" + str(payload) + ",payload.zip",
            "/resource:" + str(environment) + ",env.zip",
            "/resource:" + str(build / "edition.txt") + ",edition.txt",
            "/resource:" + str(build / "version.txt") + ",version.txt",
            "/resource:" + str(repo / "installer/assets/pepper-splash.gif") + ",splash.gif",
            str(assembly),
            str(repo / "installer/PortableInstaller.cs"),
        ],
        check=True,
    )
    return target


if __name__ == "__main__":
    from boardmodeler.build_flavor import BOB_ONLY

    print(
        package(Path(__file__).resolve().parents[1], "SpiceMakerBob" if BOB_ONLY else "SpiceMaker")
    )
