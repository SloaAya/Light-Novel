@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (echo ERROR: Python not found. Install Python 3.8+ and add to PATH. & pause & exit /b 1)

rem ---- 可选：设置访问口令（口令不要写进仓库）----
rem 两档身份：管理员能看到并用「已读完」功能；访客只能浏览/下载，看不到该入口。
rem 都不设 = 免密：本机(127.0.0.1)算管理员，其余来源算访客（安全下限，但不是零配置就等于公开了书库）。
rem set LN_OPDS_USER=ln
rem set LN_OPDS_PASS=改成你自己的口令
rem set LN_OPDS_GUEST_USER=guest
rem set LN_OPDS_GUEST_PASS=改成访客口令

rem ---- 可选：改端口（默认 8080）----
rem set LN_OPDS_PORT=8080

echo.
echo 启动 OPDS 书源服务，手机阅读器订阅后即可浏览并下载书库。
echo 停止服务请按 Ctrl+C
echo.
"%PY%" -m lightnovel opds %*
pause
