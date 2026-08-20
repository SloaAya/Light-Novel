@echo off
REM ============================================================
REM  单次同步：复制书籍 + 提交并推送到 GitHub，然后退出。
REM  用法：双击本文件运行（会显示命令行窗口与日志）。
REM ============================================================
cd /d D:\Light-Novel
if exist "%~dp0sync_lightnovel.py" (
    python "%~dp0sync_lightnovel.py" --once
) else (
    python sync_lightnovel.py --once
)
echo.
echo 按任意键退出...
pause >nul
