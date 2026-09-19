@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem =====================================================================
rem  Restart the OPDS service as a DETACHED background process:
rem    - started via pythonw.exe / DETACHED_PROCESS (no console attached)
rem    - so closing THIS window (x or Ctrl+C) does NOT stop the service
rem  It stops the old instance first, then waits and health-checks.
rem
rem  Interpreter discovery lives in _find_python.bat (shared helper).
rem  For the full online launcher - service + tunnel + watchdog daemon -
rem  use launch_online.bat instead.
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
"%RUN%" %RUNARGS% restart --port 8080 %*
set RC=%ERRORLEVEL%

echo.
echo ---------------------------------------------------------------------
if "%RC%"=="0" (
    echo The service is now running in the background.
    echo You can close this window now - x or Ctrl+C will not stop it.
) else (
    echo Startup failed - see the log tail printed above.
)
echo   Status : run this file again ^(it restarts and prints status^)
echo   Stop   : launchers\stop_opds.bat   or   python -m lightnovel stop
echo ---------------------------------------------------------------------
pause
exit /b %RC%
