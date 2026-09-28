@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "SPICE_MAKER_ROOT=%~dp0"
cd /d "%SPICE_MAKER_ROOT%" 2>nul
if errorlevel 1 (
    echo Spice Maker cannot open its folder. Move the extracted folder to a writable location.
    goto :failed
)

echo Spice Maker setup
echo This will check for CPython 3.14, make a .venv in this folder, download
echo hash-checked Python packages, and ask for your LTspice and provider settings.
echo If Python is missing, you can choose a python.org download. LTspice is never downloaded.
echo.

set "HAD_ARGUMENTS="
if not "%~1"=="" set "HAD_ARGUMENTS=1"
set "AUTO_YES="
set "NO_INSTALL_PYTHON="
set "REMOVE_SHORTCUT="
set "SHORTCUT_DIR="
:scan_arguments
if "%~1"=="" goto :arguments_done
if /I "%~1"=="--yes" set "AUTO_YES=1"
if /I "%~1"=="--no-install-python" set "NO_INSTALL_PYTHON=1"
if /I "%~1"=="--remove" set "REMOVE_SHORTCUT=1"
if /I "%~1"=="--shortcut-dir" goto :read_shortcut_dir
shift
goto :scan_arguments
:read_shortcut_dir
shift
if "%~1"=="" (
    echo --shortcut-dir needs a folder path.
    goto :failed
)
set "SHORTCUT_DIR=%~1"
shift
goto :scan_arguments
:arguments_done
if defined REMOVE_SHORTCUT goto :remove_shortcut

set "ROOT_WITHOUT_SLASH=%SPICE_MAKER_ROOT:~0,-1%"
if not "%ROOT_WITHOUT_SLASH:~200%"=="" goto :path_too_long

rem Never let either Windows launcher install Python while we only check what is present.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=0"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="

powershell -NoProfile -Command "if ([Environment]::OSVersion.Version.Major -ge 10) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo Spice Maker needs Windows 10 or 11. This version of Windows is unsupported.
    goto :failed
)

set "WINDOWS_ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "WINDOWS_ARCH=%PROCESSOR_ARCHITEW6432%"
if /I "%WINDOWS_ARCH%"=="AMD64" (
    set "PIN_ARCH=amd64"
    set "EXPECTED_PLATFORM=win-amd64"
)
if /I "%WINDOWS_ARCH%"=="ARM64" (
    set "PIN_ARCH=arm64"
    set "EXPECTED_PLATFORM=win-arm64"
)
if not defined PIN_ARCH (
    echo Unsupported Windows architecture: %WINDOWS_ARCH%.
    echo Spice Maker needs Windows 10 or 11 on x64 or ARM64.
    goto :failed
)

set "PIN_FILE=%SPICE_MAKER_ROOT%tools\python-install-pins.txt"
rem Tests may supply a local pin and an isolated PEP 514 registry root.
if defined SPICE_MAKER_TEST_PINS set "PIN_FILE=%SPICE_MAKER_TEST_PINS%"
call :read_pin
if errorlevel 1 goto :failed

call :find_python
if defined PYTHON_EXE goto :bootstrap
if defined NO_INSTALL_PYTHON goto :python_missing

echo Python 3.14 was not found. Spice Maker needs it.
echo Download python-3.14.7-%PIN_ARCH%.exe ^(%PIN_MB% MB^) from python.org,
echo check its SHA-256, and install it for your user only?
echo No administrator rights, no PATH change; your other Pythons are untouched.
echo Installing accepts the Python license ^(PSF-2.0^).
if defined AUTO_YES goto :install_python
set "ANSWER="
set /p "ANSWER=Install Python now? [y/N] "
if /I not "%ANSWER%"=="y" goto :python_missing

:install_python
call :download_and_install
if errorlevel 1 goto :failed

rem The current terminal's PATH is stale after installation. Use PEP 514 again.
set "PYTHON_EXE="
call :find_registry_python
if not defined PYTHON_EXE (
    echo Python installation finished, but no matching registry entry was found.
    echo Check the install log and run Setup.cmd again: %INSTALL_LOG%
    goto :failed
)

:bootstrap
echo Using %PYTHON_EXE%
"%PYTHON_EXE%" -I "%SPICE_MAKER_ROOT%tools\bootstrap.py" %*
if errorlevel 1 goto :failed
echo.
echo Setup finished.
if not defined HAD_ARGUMENTS pause
exit /b 0

:python_missing
echo.
echo Python 3.14 is required. Install it from https://www.python.org/downloads/release/python-3147/
echo Then run Setup.cmd again.
goto :failed
:path_too_long
echo This folder path is over 200 characters. Move the extracted folder closer to the drive root.
goto :failed
:failed
echo.
echo Setup stopped before completion.
if not defined HAD_ARGUMENTS pause
exit /b 1

:remove_shortcut
rem Shortcut removal works even when Python has been uninstalled. Paths go through
rem environment variables so quotes and non-ASCII folder names remain literal.
set "SPICE_SHORTCUT_DIR=%SHORTCUT_DIR%"
powershell -NoProfile -NonInteractive -Command ^
"try { $name='Spice Maker.lnk'; ^
$state=Join-Path $env:SPICE_MAKER_ROOT 'data\shortcut.json'; ^
if($env:SPICE_SHORTCUT_DIR){$target=Join-Path $env:SPICE_SHORTCUT_DIR $name} ^
elseif(Test-Path -LiteralPath $state){$target=[string](Get-Content -LiteralPath $state -Raw -Encoding UTF8 -ErrorAction Stop ^| ConvertFrom-Json -ErrorAction Stop).path} ^
else{$target=Join-Path ([Environment]::GetFolderPath('Desktop')) $name}; ^
if([IO.Path]::GetFileName($target) -cne $name){throw 'Unexpected shortcut path'}; ^
if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Force -ErrorAction Stop}; ^
if(Test-Path -LiteralPath $state){Remove-Item -LiteralPath $state -Force -ErrorAction Stop}; ^
Write-Host ('Shortcut removed: ' + $target); exit 0 ^
} catch { Write-Host ('Could not remove shortcut: ' + $_.Exception.Message); exit 1 }"
if errorlevel 1 goto :failed
exit /b 0

:read_pin
if not exist "%PIN_FILE%" (
    echo Python installer pin file is missing: %PIN_FILE%
    exit /b 1
)
set "PIN_URL="
set "PIN_HASH="
set "PIN_BYTES="
set "PIN_MB="
for /f "usebackq eol=# tokens=1-5 delims=|" %%A in ("%PIN_FILE%") do if /I "%%A"=="%PIN_ARCH%" (
    set "PIN_URL=%%B"
    set "PIN_HASH=%%C"
    set "PIN_BYTES=%%D"
    set "PIN_MB=%%E"
)
if not defined PIN_URL goto :bad_pin
if not defined PIN_HASH goto :bad_pin
if not defined PIN_BYTES goto :bad_pin
if not defined PIN_MB goto :bad_pin
exit /b 0
:bad_pin
echo Python installer pin for %PIN_ARCH% is missing or incomplete: %PIN_FILE%
exit /b 1

:find_python
set "PYTHON_EXE="
call :find_registry_python
if defined PYTHON_EXE exit /b 0
rem An isolated test registry root also prevents a test from probing this PC's py command.
if defined SPICE_MAKER_TEST_REGISTRY_ROOT exit /b 1
for /f "delims=" %%P in ('py -3.14 -I -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE (
    set "CANDIDATE=%%P"
    call :probe_candidate
)
if defined PYTHON_EXE exit /b 0
exit /b 1

:find_registry_python
if defined SPICE_MAKER_TEST_REGISTRY_ROOT (
    set "REGISTRY_ROOT=%SPICE_MAKER_TEST_REGISTRY_ROOT%"
    call :probe_registry_root
    exit /b 0
)
set "REGISTRY_ROOT=HKCU\Software\Python\PythonCore"
call :probe_registry_root
if defined PYTHON_EXE exit /b 0
set "REGISTRY_ROOT=HKLM\Software\Python\PythonCore"
call :probe_registry_root
exit /b 0

:probe_registry_root
for %%T in (3.14 3.14-arm64) do (
    for /f "tokens=1,2,*" %%A in ('reg query "%REGISTRY_ROOT%\%%T\InstallPath" /v ExecutablePath /reg:64 2^>nul') do (
        if /I "%%A"=="ExecutablePath" if /I "%%B"=="REG_SZ" if not defined PYTHON_EXE (
            set "CANDIDATE=%%C"
            call :probe_candidate
        )
    )
)
exit /b 0

:probe_candidate
if not exist "%CANDIDATE%" exit /b 1
rem Require Python to execute our code. A Store alias or other executable returning 0 is not enough.
"%CANDIDATE%" -I -c "import sys,sysconfig; ok=sys.implementation.name=='cpython' and sys.version_info[:2]==(3,14) and sysconfig.get_platform()=='%EXPECTED_PLATFORM%'; sys.exit(73 if ok else 1)" >nul 2>nul
if not "%errorlevel%"=="73" exit /b 1
set "PYTHON_EXE=%CANDIDATE%"
exit /b 0

:download_and_install
set "TEMP_DIR=%SPICE_MAKER_ROOT%data\temp"
if not exist "%TEMP_DIR%" mkdir "%TEMP_DIR%" 2>nul
if not exist "%TEMP_DIR%" (
    echo Cannot write setup files in this folder: %TEMP_DIR%
    exit /b 1
)
set "WRITE_CHECK=%TEMP_DIR%\write-check.tmp"
type nul >"%WRITE_CHECK%" 2>nul
if not exist "%WRITE_CHECK%" (
    echo Cannot write setup files in this folder: %TEMP_DIR%
    exit /b 1
)
del /q "%WRITE_CHECK%" >nul 2>nul

set "INSTALLER=%TEMP_DIR%\python-3.14.7-%PIN_ARCH%.exe"
set "INSTALL_LOG=%TEMP_DIR%\python-3.14.7-%PIN_ARCH%-install.log"
if exist "%INSTALLER%" del /q "%INSTALLER%" >nul 2>nul
echo Downloading %PIN_URL%
curl.exe --fail --location --retry 2 --connect-timeout 20 --max-time 300 --output "%INSTALLER%" "%PIN_URL%" >nul 2>nul
if errorlevel 1 (
    if exist "%INSTALLER%" del /q "%INSTALLER%" >nul 2>nul
    echo curl.exe could not download Python; trying PowerShell.
    set "SPICE_PY_URL=%PIN_URL%"
    set "SPICE_PY_DEST=%INSTALLER%"
    powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri $env:SPICE_PY_URL -OutFile $env:SPICE_PY_DEST -ErrorAction Stop ^| Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
    if errorlevel 1 (
        echo Python download failed. Check your internet connection or HTTPS_PROXY setting.
        if exist "%INSTALLER%" del /q "%INSTALLER%" >nul 2>nul
        exit /b 1
    )
)
if not exist "%INSTALLER%" (
    echo Python download did not create a file.
    exit /b 1
)
for %%F in ("%INSTALLER%") do set "DOWNLOADED_BYTES=%%~zF"
if not "%DOWNLOADED_BYTES%"=="%PIN_BYTES%" (
    echo Python download has the wrong size. It will not be run.
    del /q "%INSTALLER%" >nul 2>nul
    exit /b 1
)
set "DOWNLOADED_HASH="
for /f "delims=" %%H in ('certutil -hashfile "%INSTALLER%" SHA256 2^>nul ^| findstr /R /C:"^[0-9A-Fa-f][0-9A-Fa-f]*$"') do set "DOWNLOADED_HASH=%%H"
if /I not "%DOWNLOADED_HASH%"=="%PIN_HASH%" (
    echo Python download failed its SHA-256 check. It will not be run.
    del /q "%INSTALLER%" >nul 2>nul
    exit /b 1
)
echo SHA-256 verified. Installing Python for the current user only.
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 AppendPath=0 AssociateFiles=0 Shortcuts=0 Include_launcher=0 Include_doc=0 Include_test=0 Include_tcltk=0 Include_tools=0 /log "%INSTALL_LOG%"
set "INSTALL_EXIT=%errorlevel%"
del /q "%INSTALLER%" >nul 2>nul
if not "%INSTALL_EXIT%"=="0" (
    echo Python installer exited with code %INSTALL_EXIT%. See log: %INSTALL_LOG%
    exit /b 1
)
exit /b 0
