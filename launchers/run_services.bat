@echo off
chcp 65001 >nul
cd /d "%~dp0.."

rem 一键启动「OPDS 书源 + 目录监控」两个常驻服务（优先用打包好的 exe，缺失则回退源码）。
rem 只做这一件事；需要看状态灯 / 单独启停，用 run_panel.bat。
rem 公网隧道（cloudflared）不在这里起：若它已在跑，8080 一恢复公网即刻可用；
rem 若隧道没跑，请改用 launchers\run_named_tunnel.bat。

set "RUN=dist\LightNovel.exe"
if exist "%RUN%" goto launch

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY goto nopython
set "RUN=%PY% -m lightnovel"
echo [提示] 未找到 dist\LightNovel.exe，改用源码运行。

:launch
echo.
echo   启动 OPDS 书源（端口 8080）...
start "LN-OPDS" /min %RUN% opds --port 8080 --no-qr
echo   启动目录监控...
start "LN-Monitor" /min %RUN% sync
echo.
echo   两个服务已在独立的最小化窗口里启动。
echo   停止：关掉那两个窗口，或运行 stop_opds.bat（只停 OPDS）。
echo   公网地址：https://ranqing.ccwu.cc/（需要 cloudflared 隧道在跑）
echo.
timeout /t 6 >nul
exit /b 0

:nopython
echo ERROR: 既没有 dist\LightNovel.exe 也没有 Python。请先打包 exe 或安装 Python 3.8+。
pause
exit /b 1
