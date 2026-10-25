@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
call :try "py -3"
if not defined PY call :try "python"
if not defined PY call :try "python3"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%~fD\python.exe"
if not defined PY (
    echo.
    echo Python 3.10+ not found. Install it from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" during setup, then run this file again.
    echo.
    pause
    exit /b 1
)
%PY% -m w3dualsub gui %*
echo.
pause
exit /b 0

:try
if not defined PY %* -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=%*"
exit /b 0
