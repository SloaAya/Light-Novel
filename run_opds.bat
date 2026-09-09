@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (echo ERROR: Python not found. Install Python 3.8+ and add to PATH. & pause & exit /b 1)

rem ---- 可选：设置访问口令（本文件与 py 文件都会进 GitHub，口令建议在这里设，且不要提交）----
rem set LN_OPDS_USER=ln
rem set LN_OPDS_PASS=改成你自己的口令

rem ---- 可选：改端口（默认 8080）----
rem set LN_OPDS_PORT=8080

echo.
echo 启动 OPDS 书源服务，手机阅读器订阅后即可浏览并下载书库。
echo 停止服务请按 Ctrl+C
echo.
"%PY%" "%~dp0opds_server.py" %*
pause
