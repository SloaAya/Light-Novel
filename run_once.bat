@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (echo ERROR: Python not found. Install Python 3.8+ and add to PATH. & pause & exit /b 1)
"%PY%" "%~dp0sync_lightnovel.py" --once
