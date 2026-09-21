<#
.SYNOPSIS
  Build Spice Maker and package it as a one-click installer with Velopack.

.DESCRIPTION
  1. Renders the pepper splash (product name and version in the title block) and the
     multi-size icon with installer\render_assets.py.
  2. Freezes the app with PyInstaller in onedir mode (Velopack cannot use --onefile):
     installer\SpiceMaker.spec packs installer\entry.py into dist\SpiceMaker\SpiceMaker.exe
     with the pepper icon and the boardmodeler package alongside it.
  3. Runs vpk pack: releases\Setup.exe — one click, no wizard pages, with the animated
     splash whose bottom 12 px Velopack paints its progress bar over.

  The installer carries the app and nothing else. It never downloads or installs
  LTspice, Bob Shell or Python: the LTspice executable path and its smoke test live in
  the app's own SETUP page, and no build step writes into an LTspice directory.
  The app reads no repository data at run time — the datasheet, the save folder and the
  models all come from the user — so nothing but the icon ships besides the package.

  One-time setup:
    uv sync --all-extras          # pyinstaller + pillow + velopack into .venv
    dotnet tool install -g vpk --version 1.2.0 # needs the .NET SDK; keep vpk on the same version
                                  # as the velopack Python package

  Run from the project root (the repo root, where pyproject.toml lives):
    .\installer\build.ps1 -Version 1.0.0
#>
param(
    [Parameter(Mandatory = $true)][string] $Version,
    [string] $Name = "",   # splash title block and the Add/Remove entry
    [string] $PackId = ""   # installs to %LocalAppData%\SpiceMaker
)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$assets = Join-Path $PSScriptRoot "assets"
$exe = "SpiceMaker"                   # must match NAME in installer\SpiceMaker.spec
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run uv sync --frozen --all-extras first; the project Python environment is required." }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must be x.y.z" }
& $python -c "import sys; assert sys.version_info[:2] == (3, 14), 'Python 3.14 is required'"
if ($LASTEXITCODE -ne 0) { throw "The build requires Python 3.14" }
$sourceVersion = (& $python -c "from boardmodeler import __version__; import tomllib; from pathlib import Path; p=tomllib.loads(Path('pyproject.toml').read_text()); assert p['project']['version']==__version__; print(__version__)")
if ($LASTEXITCODE -ne 0 -or $Version -ne $sourceVersion) { throw "Installer version must match source and pyproject.toml ($sourceVersion)." }
$bobOnly = (& $python -c "from boardmodeler.build_flavor import BOB_ONLY; print(int(BOB_ONLY))") -eq "1"
if (-not $Name) { $Name = if ($bobOnly) { "Spice Maker Bob" } else { "Spice Maker" } }
if (-not $PackId) { $PackId = if ($bobOnly) { "SpiceMakerBob" } else { "SpiceMaker" } }


function Invoke-Step([string] $what, [scriptblock] $cmd) {
    Write-Host "==> $what"
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

Push-Location $repo
try {
    Invoke-Step "Render splash and icon" {
        & $python "$PSScriptRoot\render_assets.py" --name $Name --version $Version --out $assets
    }

    Invoke-Step "Freeze the app with PyInstaller" {
        # The spec carries every analysis option (entry script, icon, data files, the
        # run-time provider imports), so each checkout uses the same declared build inputs.
        & $python "$PSScriptRoot\freeze.py"
    }

    Invoke-Step "Verify the frozen GUI opens" {
        & $python "$PSScriptRoot\verify_gui.py" "dist\$exe\$exe.exe" `
            --screenshot "build\gui-startup.png"
    }

    Invoke-Step "Package with Velopack" {
        # vpk refuses a version that is already in releases\; rebuilding a version
        # replaces that version's artifacts instead of failing.
        $releaseRoot = [IO.Path]::GetFullPath((Join-Path $repo "releases"))
        $versionPattern = '^' + [regex]::Escape("$PackId-$Version-") + '(full\.nupkg|delta\.nupkg|Windows-x64\.zip)$'
        foreach ($artifact in (Get-ChildItem -LiteralPath $releaseRoot -File -ErrorAction SilentlyContinue)) {
            if ($artifact.Name -match $versionPattern) {
                $resolvedArtifact = [IO.Path]::GetFullPath($artifact.FullName)
                if ([IO.Path]::GetDirectoryName($resolvedArtifact) -ne $releaseRoot) { throw "Artifact escapes releases directory" }
                Remove-Item -LiteralPath $resolvedArtifact -Force
            }
        }
        vpk pack `
            --packId $PackId `
            --packTitle $Name `
            --packVersion $Version `
            --packDir "dist\$exe" `
            --mainExe "$exe.exe" `
            --icon "$assets\pepper.ico" `
            --splashImage "$assets\pepper-splash.gif" `
            --splashProgressColor "#B52A1F" `
            --outputDir releases
    }

    # Velopack 1.2 names the bundle "<packId>-<channel>-Setup.exe"; publish the same
    # bytes under the plain name the download link uses. The canonical file stays put
    # because assets.win.json lists it.
    Invoke-Step "Publish releases\Setup.exe" {
        Copy-Item "releases\$PackId-win-Setup.exe" "releases\Setup.exe" -Force
    }
    # The download zip contains the original animated installer, unchanged.
    $bundle = Join-Path $repo "build/download-$PackId"
    New-Item -ItemType Directory -Force -Path $bundle | Out-Null
    Copy-Item "releases/$PackId-win-Setup.exe" "$bundle/Install.exe" -Force
    @"
$Name $Version for Windows x64

Extract this zip, then double-click Install.exe. The pepper animation plays during setup.
Python is included. Open SETUP once to select LTspice and save your API key.
LTspice and (for IBM Bob) Bob Shell must be installed separately.
The API key stays in a local file encrypted for this Windows user; no account login is needed in this app.
GO sends the selected datasheet and model text to the chosen provider.
This build is unsigned. Check the publisher/source and the SHA256 before running it.
"@ | Set-Content "$bundle/Read me.txt" -Encoding utf8
    $hash = (Get-FileHash "$bundle/Install.exe" -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  Install.exe" | Set-Content "$bundle/SHA256SUMS.txt" -Encoding ascii
    Compress-Archive -LiteralPath "$bundle/Install.exe", "$bundle/Read me.txt", "$bundle/SHA256SUMS.txt" `
        -DestinationPath "releases/$PackId-$Version-Windows-x64.zip" -Force
    # Track the actual installer in the repository so Code -> Download ZIP includes it.
    Copy-Item "$bundle/Install.exe" "$repo/Install.exe" -Force
    Copy-Item "$bundle/Read me.txt" "$repo/INSTALL.txt" -Force
    Copy-Item "$bundle/SHA256SUMS.txt" "$repo/SHA256SUMS.txt" -Force
} finally {
    Pop-Location
}
Write-Host "Done. Setup.exe and the update packages are in .\releases"
