@echo off
setlocal enabledelayedexpansion
REM Stop AutomotiveGUI dashboard

set "SCRIPT_DIR=%~dp0"
set "PID_FILE=%SCRIPT_DIR%.dashboard.pid"

if not exist "%PID_FILE%" (
    echo [FAIL] Dashboard not running
    exit /b 1
)

set /p PID=<"%PID_FILE%"

tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul 2>&1
if !errorlevel! neq 0 (
    echo [OK] Dashboard not running (cleaning stale PID)
    del "%PID_FILE%" 2>nul
    exit /b 0
)

taskkill /F /T /PID !PID! >nul 2>&1

ping -n 2 127.0.0.1 >nul

tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul 2>&1
if !errorlevel! equ 0 (
    echo [FAIL] Stop failed PID: !PID!
    exit /b 1
)

echo [OK] Dashboard stopped PID: !PID!
del "%PID_FILE%" 2>nul

endlocal
