@echo off
REM ============================================================
REM  后台运行监控（无窗口）。日志见 D:\Light-Novel\.autosync\sync.log
REM  用法：双击本文件即可在后台启动实时监控。
REM  停止：在任务管理器中结束 pythonw 进程，或删除 .autosync\monitor.lock。
REM ============================================================
cd /d D:\Light-Novel
if exist "%~dp0sync_lightnovel.py" (
    start "" pythonw "%~dp0sync_lightnovel.py"
) else (
    start "" pythonw sync_lightnovel.py
)
echo 监控已在后台启动，可查看 .autosync\sync.log 了解进度。
timeout /t 2 >nul
