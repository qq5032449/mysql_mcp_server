@echo off
rem 启动 mysql_mcp_server（SSE 模式，后台运行）
rem 可通过环境变量覆盖：MCP_SSE_PORT、MCP_SSE_HOST、ADMIN_TOKEN、MYSQL_MCP_CONFIG_DIR
setlocal

set APP_DIR=%~dp0
set BIN=%APP_DIR%mysql_mcp_server.exe
set PID_FILE=%APP_DIR%mysql_mcp_server.pid
set LOG_DIR=%APP_DIR%logs
set CONFIG_DIR=%MYSQL_MCP_CONFIG_DIR%
if "%CONFIG_DIR%"=="" set CONFIG_DIR=%APP_DIR%config

if not exist "%BIN%" (
    echo 错误：找不到可执行文件 %BIN%
    exit /b 1
)

rem 已在运行则跳过
if exist "%PID_FILE%" (
    for /f %%i in ('type "%PID_FILE%"') do set OLD_PID=%%i
    tasklist /FI "PID eq %OLD_PID%" 2>nul | find /i "%OLD_PID%" >nul
    if not errorlevel 1 (
        echo 已在运行（PID %OLD_PID%），如需重启请先执行 stop.bat
        exit /b 0
    )
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"

if "%MCP_SSE_PORT%"=="" set MCP_SSE_PORT=8000
if "%MCP_SSE_HOST%"=="" set MCP_SSE_HOST=0.0.0.0
rem 管理页面局域网访问令牌（不设置则管理页面仅限本机访问）
rem set ADMIN_TOKEN=change-me

set MCP_TRANSPORT=sse
set MYSQL_MCP_CONFIG_DIR=%CONFIG_DIR%

rem Start-Process 后台启动并记录 PID（onefile 引导进程）
for /f %%i in ('powershell -NoProfile -Command "(Start-Process -FilePath '%BIN%' -WindowStyle Hidden -RedirectStandardOutput '%LOG_DIR%\server.log' -RedirectStandardError '%LOG_DIR%\server.err.log' -PassThru).Id"') do set PID=%%i
echo %PID%> "%PID_FILE%"

timeout /t 2 /nobreak >nul
tasklist /FI "PID eq %PID%" 2>nul | find /i "%PID%" >nul
if errorlevel 1 (
    echo 错误：进程启动失败，请查看 %LOG_DIR%\server.log 与 server.err.log
    del /f "%PID_FILE%" >nul 2>&1
    exit /b 1
)

echo 已启动 PID %PID%
echo   管理页面 : http://localhost:%MCP_SSE_PORT%/admin/
echo   MCP SSE  : http://localhost:%MCP_SSE_PORT%/sse?alias=别名
echo   日志     : %LOG_DIR%\server.log
endlocal
