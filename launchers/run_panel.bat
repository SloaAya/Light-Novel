@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem =====================================================================
rem  Graphical control panel (Tkinter) - the only double-click entry to
rem  the GUI. It runs "python -m lightnovel ui" in pythonw.exe so no
rem  console window appears.
rem
rem  The panel itself can start/stop the OPDS service, the directory
rem  monitor, run a sync/mirror once and open the tunnel wizard - that is
rem  why this launcher is kept (the CLI equivalents still exist, but the
rem  GUI has no replacement).
rem
rem  Interpreter: shared helper _find_python.bat. When it resolves to a
rem  python.exe path, the same folder's pythonw.exe is preferred so the
rem  panel does not bring up a console; "-m lightnovel ui" is then run
rem  detached from this window with start.
rem
rem  If the panel fails to appear, run this in a terminal to see errors:
rem      python -m lightnovel ui
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

rem Prefer the GUI-subsystem interpreter next to python.exe, if present.
set "PANEL=%RUN%"
for %%F in ("%RUN%") do (
    if /i "%%~nxF"=="python.exe" if exist "%%~dpFpythonw.exe" set "PANEL=%%~dpFpythonw.exe"
)

start "" "%PANEL%" %RUNARGS% ui
exit /b 0
