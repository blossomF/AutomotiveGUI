@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
set "PY=%ROOT%server\autogui\.venv\Scripts\python.exe"
set "DASHBOARD=%ROOT%dist\dashboard_server.py"
set "PROCESS_SCRIPT=%ROOT%dist\dashboard_process.ps1"

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
if not defined MODE call :choose_mode
if errorlevel 1 goto :choice_failed

if not exist "%PY%" (
    echo [FAIL] Python not found: %PY%
    echo Create the virtual environment in server\autogui first.
    goto :startup_failed
)
if not exist "%DASHBOARD%" (
    echo [FAIL] Dashboard not found: %DASHBOARD%
    goto :startup_failed
)

if exist "%ROOT%.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do set "%%A=%%B"
)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROCESS_SCRIPT%" -Action status >nul 2>&1
if not errorlevel 1 (
    echo [FAIL] A background dashboard is already running. Use stop.bat first.
    goto :startup_failed
)

if /I "%MODE%"=="foreground" goto :foreground

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROCESS_SCRIPT%" -Action start
if errorlevel 1 goto :startup_failed
echo [OK] Background dashboard: http://127.0.0.1:8777
echo [INFO] Run stop.bat to stop it. Logs: %ROOT%.dashboard.log
if "%~1"=="" ping -n 3 127.0.0.1 >nul
exit /b 0

:foreground
echo [INFO] Foreground dashboard: http://127.0.0.1:8777
echo [INFO] Logs appear here. Press Ctrl+C to stop.
"%PY%" -u "%DASHBOARD%"
exit /b %errorlevel%

:choose_mode
echo Select startup mode:
echo   [F] Foreground: live logs here; Ctrl+C stops the server.
echo   [B] Background: return to CMD; stop.bat stops the server.
choice /C FB /N /M "Press F or B: "
if errorlevel 3 exit /b 1
if errorlevel 2 (
    set "MODE=background"
    exit /b 0
)
if errorlevel 1 (
    set "MODE=foreground"
    exit /b 0
)
exit /b 1

:choice_failed
echo.
echo [FAIL] No valid choice was received. Server was not started.
echo Run start.bat again and press F or B.
if "%~1"=="" pause
exit /b 2

:startup_failed
echo [FAIL] Server was not started.
if "%~1"=="" pause
exit /b 1
