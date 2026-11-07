@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 量化交易系统 - 一键启动中心

echo ============================================================
echo      🚀 正在一键启动量化交易系统 (Watchdog + Dashboard)
echo ============================================================
echo.

echo [1/3] 正在启动交易机器人看门狗守护引擎...
start "Trading Bot - Watchdog" cmd /k "cd /d "%~dp0" && run_with_watchdog.bat"

echo [2/3] 正在启动 Web 监控面板服务 (端口 8080)...
start "Trading Bot - Dashboard" cmd /k "cd /d "%~dp0" && dashboard.bat"

echo [3/3] 正在唤醒浏览器打开看板...
timeout /t 2 >nul
start http://127.0.0.1:8080

echo.
echo ============================================================
echo      ✅ 系统已启动完毕！
echo      - 交易机器人窗口：已在看门狗守护下巡检运行 (支持故障自愈)
echo      - 网页监控面板：http://127.0.0.1:8080
echo      - 保持弹出的黑色终端窗口运行即可，本引导窗口 5 秒后自动关闭
echo ============================================================
timeout /t 5
exit
