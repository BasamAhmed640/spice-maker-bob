<#
Build the frozen app and animated portable installer. Requires uv sync --all-extras
and the .NET Framework compiler included in Windows. Installs only beside Install.exe.
#>
param(
    [Parameter(Mandatory = $true)][string] $Version,
    [string] $Name = "",   # splash title block and the Add/Remove entry
    [string] $PackId = ""   # identifies the edition
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

    Invoke-Step "Package the folder-local animated installer" {
        & $python "$PSScriptRoot\package_portable.py"
    }
    Invoke-Step "Verify portable install, update and fresh-copy behavior" {
        & $python "$PSScriptRoot\verify_portable.py"
    }
    New-Item -ItemType Directory -Force -Path releases | Out-Null
    Copy-Item "$repo/Install.exe" "releases/Setup.exe" -Force
    # The download zip contains the original animated installer, unchanged.
    $bundle = Join-Path $repo "build/download-$PackId"
    New-Item -ItemType Directory -Force -Path $bundle | Out-Null
    Copy-Item "$repo/Install.exe" "$bundle/Install.exe" -Force
    @"
$Name $Version for Windows x64

Extract this zip, then double-click Install.exe. The pepper animation plays during setup.
Python is included. First launch requires SETUP: choose LTspice, a model folder inside this folder, and your key.
The app is unpacked into app/ here. Start.cmd opens it next time.
Settings, encrypted key, temporary work, logs and models stay inside this extracted folder.
No registry installation, Start Menu entry, AppData settings, or Credential Manager entries.
Delete this entire extracted folder for a fresh start. Reinstalling in it preserves data/.
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
Write-Host "Done. Portable Setup.exe and the download ZIP are in .\releases"
