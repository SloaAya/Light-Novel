@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (echo ERROR: Python not found. & pause & exit /b 1)

echo.
echo ============================================================
echo   Cloudflare 固定域名隧道 配置向导
echo ------------------------------------------------------------
echo   前置条件：
echo     1. 你有一个域名，并且 DNS 托管在 Cloudflare（免费即可）
echo     2. 过程中会打开浏览器让你登录 Cloudflare 授权
echo ============================================================
echo.
pause

"%PY%" "%~dp0setup_named_tunnel.py"
echo.
pause
