@echo off
cd /d "%~dp0.."
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
rem Admin (can use the "finished" feature) / guest (browse + download only):
rem set LN_OPDS_USER=your_user
rem set LN_OPDS_PASS=your_password
rem set LN_OPDS_GUEST_USER=guest
rem set LN_OPDS_GUEST_PASS=guest_password

echo Starting Light-Novel OPDS service (fixed-domain tunnel)...
echo If the tunnel is not configured yet, run setup_named_tunnel.bat first.
echo.
echo The console window will auto-hide once the tunnel is connected.
echo To stop the service, run stop_opds.bat
echo.
"%PY%" -m lightnovel opds --tunnel named --no-qr --hide-window
