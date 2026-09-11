@echo off
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (
    echo ERROR: Python not found. Install Python 3.8+ and add to PATH.
    pause
    exit /b 1
)

rem Optional overrides (uncomment to use):
rem set LN_OPDS_PORT=8080
rem set LN_OPDS_USER=your_user
rem set LN_OPDS_PASS=your_password

echo Starting Light-Novel OPDS service (fixed-domain tunnel)...
echo If the tunnel is not configured yet, run setup_named_tunnel.bat first.
echo.
echo The console window will auto-hide once the tunnel is connected.
echo To stop the service, run stop_opds.bat
echo.
"%PY%" "%~dp0opds_server.py" --tunnel named --no-qr --hide-window
