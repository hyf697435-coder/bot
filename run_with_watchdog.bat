@echo off
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title Trading Bot - Watchdog Supervisor (守护进程)

:run_loop
cls
echo ========================================================
echo     Trading Bot v2.1 (Multi-Coin + Risk Guard)
echo     看门狗守护自愈监控启动中...
echo     [提示] 保持此窗口常驻运行即可
echo ========================================================
python watchdog.py

echo.
echo [警告] 看门狗进程意外退出，将在 5 秒后自动重新拉起自愈...
echo 如需手动终止，请直接关闭本窗口或按 Ctrl+C
timeout /t 5
goto :run_loop
