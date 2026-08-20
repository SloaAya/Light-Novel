@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ---- Find a Python interpreter ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo [Sync] Copying book, committing and pushing once...
%PY% "%~dp0sync_lightnovel.py" --once
echo.
echo [Done] Exit code: %errorlevel%
pause
