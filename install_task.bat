@echo off
chcp 65001 >nul
echo ============================================
echo   美股异动监控 - 安装Windows定时任务
echo ============================================
echo.

REM 自动获取 pythonw.exe 路径
for /f "delims=" %%i in ('where python') do set PYEXE=%%i
set "PYW=%PYEXE:python.exe=pythonw.exe%"

if not exist "%PYW%" (
    echo [错误] 未找到 pythonw.exe: %PYW%
    pause
    exit /b 1
)

set "SCRIPT=%~dp0monitor.py"
echo Python:  %PYW%
echo 脚本:    %SCRIPT%
echo.

REM 删除旧任务（如果存在）
schtasks /delete /tn "USStockMonitor" /f >nul 2>&1

REM 创建每5分钟运行一次的任务，全天运行（脚本内部判断交易时段）
schtasks /create /tn "USStockMonitor" /tr "\"%PYW%\" \"%SCRIPT%\"" /sc minute /mo 5 /f

if %errorlevel% equ 0 (
    echo.
    echo [成功] 定时任务已创建！
    echo 任务名: USStockMonitor
    echo 频率:   每5分钟
    echo.
    echo 查看任务: schtasks /query /tn "USStockMonitor"
    echo 删除任务: schtasks /delete /tn "USStockMonitor" /f
    echo 立即运行: schtasks /run /tn "USStockMonitor"
) else (
    echo.
    echo [失败] 任务创建失败，请以管理员身份运行此脚本
)
echo.
pause
