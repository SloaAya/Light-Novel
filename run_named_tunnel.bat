@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (echo ERROR: Python not found. & pause & exit /b 1)

rem ---- 可用环境变量覆盖（在本文件里 set 即可，不要写进被同步的 .py）----
rem set LN_OPDS_PORT=8080
rem set LN_TUNNEL_NAME=ln-opds
rem set LN_OPDS_USER=ln
rem set LN_OPDS_PASS=你的口令

echo.
echo 启动 OPDS 书源（固定域名隧道模式）
echo 若尚未配置过固定域名，请先运行 setup_named_tunnel.bat
echo 停止服务请按 Ctrl+C
echo.
"%PY%" "%~dp0opds_server.py" --tunnel named
pause
