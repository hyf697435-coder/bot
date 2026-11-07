@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 量化交易系统 - 一键停止

echo ============================================================
echo      🛑 正在一键停止量化交易系统...
echo ============================================================
echo.

echo 正在停止看门狗与交易机器人 (watchdog.py / main.py)...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*watchdog.py*' -or $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo 正在停止监控面板服务 (dashboard.py)...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*dashboard.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo ============================================================
echo      ✅ 所有相关进程已安全停止！
echo ============================================================
timeout /t 3
exit
