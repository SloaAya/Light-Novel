@echo off
rem =====================================================================
rem  Shared helper: locate a Python that REALLY runs.
rem
rem  Sets for the caller (this file must NOT use setlocal):
rem     RUN      = command / path to execute   (empty when nothing found)
rem     RUNARGS  = extra args before the subcommand, e.g. "-m lightnovel"
rem
rem  Why not simply "where python": the Microsoft Store alias is found by
rem  where but exits with 9009 the moment it is executed, so every
rem  candidate here has to survive a real one-liner.
rem
rem  Preference order:
rem     1. a packaged exe (no Python needed at all)
rem     2. LN_PYTHON override
rem     3. py / python / python3 from PATH
rem     4. portable & common installs **that have Pillow** (cover
rem        thumbnails need it: without Pillow every cover falls back to
rem        the full-size original, ~18 MB instead of ~1.3 MB per page)
rem     5. fallback: first working interpreter, Pillow or not
rem
rem  ASCII only, on purpose: cmd parses .bat byte-wise and UTF-8 Chinese
rem  text shifts line boundaries, which makes cmd run comment fragments.
rem =====================================================================

set "RUN="
set "RUNARGS="

if exist "dist\LightNovel.exe" (
    set "RUN=dist\LightNovel.exe"
    goto done
)

if defined LN_PYTHON if exist "%LN_PYTHON%" (
    set "RUN=%LN_PYTHON%"
    set "RUNARGS=-m lightnovel"
    goto done
)

for %%C in (py python python3) do (
    if not defined RUN %%C -c "import sys" >nul 2>&1 && set "RUN=%%C" && set "RUNARGS=-m lightnovel"
)

if not defined RUN (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if not defined RUN if exist "%%~fD\python.exe" (
            "%%~fD\python.exe" -c "import PIL" >nul 2>&1 && set "RUN=%%~fD\python.exe"
        )
    )
)
if not defined RUN (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined RUN if exist "%%~fD\python.exe" (
            "%%~fD\python.exe" -c "import PIL" >nul 2>&1 && set "RUN=%%~fD\python.exe"
        )
    )
)
if not defined RUN if exist "%~dp0..\.venv\Scripts\python.exe" set "RUN=%~dp0..\.venv\Scripts\python.exe"
if not defined RUN (
    for /d %%D in ("C:\Python3*") do (
        if not defined RUN if exist "%%~fD\python.exe" (
            "%%~fD\python.exe" -c "import PIL" >nul 2>&1 && set "RUN=%%~fD\python.exe"
        )
    )
)

if not defined RUN (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
        if not defined RUN if exist "%%~fD\python.exe" set "RUN=%%~fD\python.exe"
    )
)
if not defined RUN (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined RUN if exist "%%~fD\python.exe" set "RUN=%%~fD\python.exe"
    )
)
if not defined RUN (
    for /d %%D in ("C:\Python3*") do (
        if not defined RUN if exist "%%~fD\python.exe" set "RUN=%%~fD\python.exe"
    )
)

:done
if defined RUN if not defined RUNARGS set "RUNARGS=-m lightnovel"
exit /b 0
