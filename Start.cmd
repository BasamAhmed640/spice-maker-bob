@echo off
setlocal
set "SPICE_MAKER_ROOT=%~dp0"
set "PYTHONPATH=%SPICE_MAKER_ROOT%src"
set "PYTHONHOME="
set "VIRTUAL_ENV="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"

if exist "%SPICE_MAKER_ROOT%.venv\Scripts\python.exe" goto launch
echo Spice Maker is not set up in this folder. Run Setup.cmd first.
exit /b 1

:launch
pushd "%SPICE_MAKER_ROOT%"
if not errorlevel 1 goto run
echo Spice Maker could not open its folder.
exit /b 1

:run
"%SPICE_MAKER_ROOT%.venv\Scripts\python.exe" -m boardmodeler.cli
set "SPICE_MAKER_EXIT=%ERRORLEVEL%"
popd
exit /b %SPICE_MAKER_EXIT%
