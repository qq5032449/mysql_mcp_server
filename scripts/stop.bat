@echo off
rem 停止 mysql_mcp_server
setlocal

set APP_DIR=%~dp0
set PID_FILE=%APP_DIR%mysql_mcp_server.pid

if not exist "%PID_FILE%" (
    echo 未找到 PID 文件（可能未在运行）
    exit /b 0
)

for /f %%i in ('type "%PID_FILE%"') do set PID=%%i

tasklist /FI "PID eq %PID%" 2>nul | find /i "%PID%" >nul
if errorlevel 1 (
    echo 进程不存在（可能已停止）
) else (
    rem /T 连同子进程一起结束（onefile 引导进程 + 实际服务进程）
    taskkill /PID %PID% /T >nul 2>&1
    echo 已停止 PID %PID%
)

del /f "%PID_FILE%" >nul 2>&1
endlocal
