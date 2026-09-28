@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "PY=%ROOT%server\autogui\.venv\Scripts\python.exe"
set "DASHBOARD=%ROOT%dist\dashboard_server.py"
set "PROCESS_SCRIPT=%ROOT%dist\dashboard_process.ps1"

if not exist "%PY%" (
    echo [FAIL] Python not found: %PY%
    echo Create the virtual environment in server\autogui first.
    exit /b 1
)
if not exist "%DASHBOARD%" (
    echo [FAIL] Dashboard not found: %DASHBOARD%
    exit /b 1
)

if exist "%ROOT%.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do set "%%A=%%B"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
chcp 65001 >nul 2>&1

set "MODE="
if /I "%~1"=="foreground" set "MODE=foreground"
if /I "%~1"=="background" set "MODE=background"
if /I "%~1"=="front" set "MODE=foreground"
if /I "%~1"=="back" set "MODE=background"
if not "%~1"=="" if not defined MODE (
    echo Usage: start.bat [foreground^|background]
    exit /b 2
)
if not defined MODE (
    echo 请选择启动方式：[F] 前台（实时日志，Ctrl+C 停止）
    echo                 [B] 后台（启动后退出，stop.bat 停止）
    choice /C FB /N /M "请选择 F 或 B："
    if errorlevel 2 (set "MODE=background") else (set "MODE=foreground")
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROCESS_SCRIPT%" -Action status >nul 2>&1
if not errorlevel 1 (
    echo [FAIL] A background dashboard is already running. Use stop.bat first.
    exit /b 1
)

if /I "%MODE%"=="foreground" goto :foreground

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROCESS_SCRIPT%" -Action start
if errorlevel 1 exit /b 1
echo [OK] Background dashboard: http://127.0.0.1:8777
echo [INFO] Run stop.bat to stop it. Logs: %ROOT%.dashboard.log
exit /b 0

:foreground
echo [INFO] Foreground dashboard: http://127.0.0.1:8777
echo [INFO] Logs appear here. Press Ctrl+C to stop.
"%PY%" -u "%DASHBOARD%"
exit /b %errorlevel%
