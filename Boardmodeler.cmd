@echo off
setlocal
set "SPICE_MAKER_ROOT=%~dp0"
set "PYTHONPATH=%SPICE_MAKER_ROOT%src"
set "PYTHONHOME="
set "VIRTUAL_ENV="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"

if exist "%SPICE_MAKER_ROOT%.venv\Scripts\python.exe" goto run
echo Spice Maker is not set up in this folder. Run Setup.cmd first. 1>&2
exit /b 1

:run
"%SPICE_MAKER_ROOT%.venv\Scripts\python.exe" -m boardmodeler.cli %*
exit /b %ERRORLEVEL%
