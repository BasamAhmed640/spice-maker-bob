"""Compile the animated, folder-local installer using Windows .NET Framework."""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path


def package(repo: Path, edition: str) -> Path:
    build = repo / "build" / "portable"
    build.mkdir(parents=True, exist_ok=True)
    payload = build / "payload.zip"
    source = repo / "dist" / "SpiceMaker"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file() and file.relative_to(source).parts[0] not in {
                "data",
                "models",
                "library",
            }:
                archive.write(file, file.relative_to(source).as_posix())
    (build / "edition.txt").write_text(edition, encoding="ascii")
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
            "/resource:" + str(build / "edition.txt") + ",edition.txt",
            "/resource:" + str(repo / "installer/assets/pepper-splash.gif") + ",splash.gif",
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
