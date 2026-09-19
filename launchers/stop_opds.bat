@echo off
rem Stop everything the online launcher started, IN THIS ORDER:
rem   1) the watchdog daemon - strictly first. If the service is stopped
rem      while the daemon is still alive, its next check (every 30 s)
rem      would bring the whole stack right back up.
rem   2) the process listening on port 8080 (the OPDS server)
rem   3) cloudflared (the tunnel client)
rem NOTE: anything else that happens to occupy port 8080 will also be
rem killed, as before.

set FOUND=0

rem --- 1) watchdog daemon (PID file written by the launcher) ---
if exist ".autosync\launcher-8080.pid" (
    for /f "delims=" %%a in ('type ".autosync\launcher-8080.pid" 2^>nul') do (
        taskkill /PID %%a /F >nul 2>&1 && echo Stopped launcher daemon - PID %%a.
    )
    del ".autosync\launcher-8080.pid" >nul 2>&1
)

rem --- 2) OPDS server ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1 && set FOUND=1
)

rem --- 3) tunnel client ---
taskkill /IM cloudflared.exe /F >nul 2>&1

if "%FOUND%"=="1" (
    echo OPDS service stopped.
) else (
    echo OPDS service is not running.
)
pause
