@echo off
rem Stop Light-Novel OPDS service:
rem   1) kill the process listening on port 8080 (the OPDS server)
rem   2) kill cloudflared (the tunnel client)
rem NOTE: anything else that happens to occupy port 8080 will also be killed.

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>&1 && set FOUND=1
)
taskkill /IM cloudflared.exe /F >nul 2>&1

if "%FOUND%"=="1" (
    echo OPDS service stopped.
) else (
    echo OPDS service is not running.
)
pause
