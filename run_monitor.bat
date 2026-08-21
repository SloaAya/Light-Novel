@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (echo ERROR: Python not found. & pause & exit /b 1)
start "" "%PY%" "%~dp0sync_lightnovel.py"
