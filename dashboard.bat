@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title Trading Bot - Web Dashboard (监控看板)

:: 自动打开浏览器
start http://127.0.0.1:8080

:dash_loop
cls
echo ========================================================
echo    Quant Trading Terminal Dashboard (Web 服务)
echo    服务运行地址: http://127.0.0.1:8080
echo    [提示] 保持此黑框常驻即可在浏览器/手机实时查看
echo ========================================================

where python >nul 2>nul
if %errorlevel% equ 0 (
    python dashboard.py --port 8080 --state state.json --log trading_bot.log
) else (
    py -3 dashboard.py --port 8080 --state state.json --log trading_bot.log
)

echo.
echo [提示] 监控看板服务已停止，将在 5 秒后重新拉起...
echo 如需彻底关闭，请直接关闭本黑框窗口或按 Ctrl+C
timeout /t 5
goto :dash_loop
