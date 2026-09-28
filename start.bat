@echo off
setlocal enabledelayedexpansion
REM Start AutomotiveGUI dashboard

set "SCRIPT_DIR=%~dp0"
set "PY=%SCRIPT_DIR%server\autogui\.venv\Scripts\python.exe"
set "DASHBOARD=%SCRIPT_DIR%dist\dashboard_server.py"
set "PID_FILE=%SCRIPT_DIR%.dashboard.pid"
set "LOG_FILE=%SCRIPT_DIR%.dashboard.log"

if exist "%PID_FILE%" (
    set /p OLD_PID=<"%PID_FILE%"
    tasklist /FI "PID eq !OLD_PID!" 2>nul | find "!OLD_PID!" >nul 2>&1
    if !errorlevel! equ 0 (
        echo [FAIL] Dashboard already running PID: !OLD_PID!
        echo   Run stop.bat first to restart
        exit /b 1
    )
    del "%PID_FILE%" 2>nul
)

if not exist "%PY%" (
    echo [FAIL] Python not found: %PY%
    echo   Create .venv in server\autogui first
    exit /b 1
)

if exist "%SCRIPT_DIR%.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
        set "%%A=%%B"
    )
)

set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul 2>&1

REM Start via PowerShell to capture the real python.exe PID
powershell -Command "$p = Start-Process -FilePath '%PY%' -ArgumentList '\"%DASHBOARD%\"' -WindowStyle Hidden -PassThru -RedirectStandardOutput '%LOG_FILE%' -RedirectStandardError '%LOG_FILE%.err'; $p.Id | Out-File -Encoding ascii -NoNewline '%PID_FILE%'"

ping -n 3 127.0.0.1 >nul

if not exist "%PID_FILE%" (
    echo [FAIL] Startup failed, log:
    type "%LOG_FILE%" 2>nul
    exit /b 1
)

set /p PID=<"%PID_FILE%"

tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul 2>&1
if !errorlevel! equ 0 (
    echo [OK] Dashboard started PID: !PID!
    echo   URL: http://localhost:8777
    echo   Log: %LOG_FILE%
    echo   Stop: stop.bat
    start "" "http://localhost:8777"
) else (
    echo [FAIL] Startup failed, log:
    type "%LOG_FILE%" 2>nul
    del "%PID_FILE%" 2>nul
    exit /b 1
)

endlocal
