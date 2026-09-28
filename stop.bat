@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROCESS_SCRIPT=%~dp0dist\dashboard_process.ps1"
if not exist "%PROCESS_SCRIPT%" (
    echo [FAIL] Process helper not found: %PROCESS_SCRIPT%
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROCESS_SCRIPT%" -Action stop
exit /b %errorlevel%
