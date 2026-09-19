@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem =====================================================================
rem  FOREground OPDS server - for watching the live log in this window.
rem
rem  This is the only intentionally foreground entry point: closing this
rem  window or pressing Ctrl+C STOPS the service. That is the point - it
rem  exists for debugging. For a detached, self-healing setup use
rem  launchers\launch_online.bat instead (it starts "python -m lightnovel
rem  launch" and hands everything to a watchdog daemon).
rem
rem  Interpreters are located by the shared helper _find_python.bat.
rem
rem  Optional overrides: set them as ENVIRONMENT variables, never in this
rem  file - the launchers are part of the git repo and credentials put
rem  here would be pushed to GitHub:
rem      set LN_OPDS_USER=your_user
rem      set LN_OPDS_PASS=your_password
rem      set LN_OPDS_PORT=8080
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
echo Services starts in the FOREGROUND - press Ctrl+C to stop it.
echo.
"%RUN%" %RUNARGS% opds %*
set RC=%ERRORLEVEL%
pause
exit /b %RC%
