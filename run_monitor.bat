@echo off
REM ============================================================
REM  后台监控：持续监听 D:\Light-Novel，发现新增/修改/删除文件即自动
REM  提交并推送到 GitHub。使用 pythonw 无命令行窗口运行，日志写入：
REM  D:\Light-Novel\.autosync\sync.log
REM
REM  用法：双击本文件即可在后台启动监控（关闭窗口不会结束进程，
REM        需结束进程请在任务管理器结束 pythonw，或删除 .autosync\monitor.lock）。
REM ============================================================
cd /d D:\Light-Novel
start "" pythonw "%~dp0sync_lightnovel.py"
echo 后台监控已启动（日志见 D:\Light-Novel\.autosync\sync.log）。
timeout /t 2 >nul
