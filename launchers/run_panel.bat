@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY="
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY (echo ERROR: Python not found. Install Python 3.8+ and add to PATH. & pause & exit /b 1)

rem 控制面板（无控制台窗口）。若启动失败想去掉 pythonw 看报错，把下面这行的
rem pythonw 换成 python 再跑一次。
start "" "%PY%" -m lightnovel ui
