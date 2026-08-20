@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

REM ---- Find a Python interpreter (prefer pythonw for no window) ----
set "PY="
where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo [Monitor] Launching background monitor (no window)...
start "" %PY% "%~dp0sync_lightnovel.py"
echo [Monitor] Started. Logs: .autosync\sync.log
timeout /t 3 >nul
