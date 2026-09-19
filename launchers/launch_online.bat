@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem =====================================================================
rem  ONLINE LAUNCHER - publishes this library at https://ranqing.ccwu.cc/
rem
rem  What it does (all of it lives in lightnovel/launcher.py):
rem    1. stops the previous watchdog daemon (if any) and the old service
rem    2. starts a WATCHDOG DAEMON: pythonw + DETACHED_PROCESS + no console
rem    3. the daemon brings up the OPDS service AND the Cloudflare tunnel,
rem       then re-checks every 30 s and rebuilds the stack if it dies
rem    4. this window waits until the local service answers 200 and the
rem       tunnel reports "connected", prints the status, and exits
rem
rem  Closing this window (x / Ctrl+C) kills only that print-and-wait step.
rem  The daemon, the service and cloudflared are separate, console-less
rem  processes - they keep running.
rem
rem  Stop everything (order matters: daemon first, otherwise its next
rem  check would bring the service back up):
rem        launchers\stop_opds.bat   or   python -m lightnovel stop
rem  Inspect:
rem        python -m lightnovel launch-status   or  .autosync\launcher.log
rem
rem  Extra args are passed through, e.g.:
rem        launch_online.bat --interval 15
rem        launch_online.bat --tunnel none        (local network only)
rem
rem  ASCII only, on purpose: cmd parses .bat byte-wise and UTF-8 Chinese
rem  text shifts the line boundaries, making cmd run comment fragments.
rem =====================================================================

call "%~dp0_find_python.bat"

if not defined RUN (
    echo ERROR: no usable Python found.
    echo.
    echo   Fix it in either way:
    echo     1^) install Python 3.8+ and tick "Add python.exe to PATH", or
    echo     2^) tell these scripts where it is, once:
    echo            setx LN_PYTHON "C:\path\to\python.exe"
    echo        then run this file again.
    echo.
    pause
    exit /b 1
)

echo Using interpreter: %RUN% %RUNARGS%
echo.
"%RUN%" %RUNARGS% launch --tunnel named --port 8080 %*
set RC=%ERRORLEVEL%

echo.
echo ---------------------------------------------------------------------
if "%RC%"=="0" (
    echo Launcher handed everything to the background daemon.
    echo You can close this window now - x or Ctrl+C will not stop anything.
) else (
    echo Startup failed - see the output above.
)
echo   Status : python -m lightnovel launch-status
echo   Stop   : launchers\stop_opds.bat
echo   Notes  : the public URL returns Cloudflare error 1033 while the
echo            tunnel is still connecting - give it a few seconds.
echo ---------------------------------------------------------------------
pause
exit /b %RC%
