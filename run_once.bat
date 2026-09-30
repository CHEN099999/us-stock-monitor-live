@echo off
chcp 65001 >nul
echo 手动运行一次监控（显示详细日志）...
echo.
python "%~dp0monitor.py"
echo.
pause
