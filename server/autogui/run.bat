@echo off
REM One-command entry for the Demo Day machine (Windows).
REM The committee sends someone to build the environment from the README alone, so
REM this script must work without uv, without make, and without a shell that isn't cmd.
REM
REM   run.bat check                     device + capture + input self-test
REM   run.bat dump-report               measure tree quality -> artifacts\dump-report.md
REM   run.bat selftest                  offline tests, no device needed
REM   run.bat "在音乐App播放《七里香》"    run a task

setlocal
cd /d "%~dp0"

if "%PYTHON%"=="" set PYTHON=python

if "%1"=="" goto :usage
if "%1"=="check"     goto :run_check
if "%1"=="selftest"  goto :run_selftest
if "%1"=="dump-report" goto :run_dump
if "%1"=="setup"     goto :run_setup
if "%1"=="mcp-check" goto :run_mcp
goto :run_task

:run_setup
  echo [setup] installing pinned dependencies
  "%PYTHON%" -m pip install -r requirements.txt || goto :fail
  echo [setup] done. set GLM_API_KEY, connect ADB, then run: run.bat check
  goto :end

:run_check
  "%PYTHON%" main.py --check %2 %3 %4 %5
  goto :end

:run_selftest
  "%PYTHON%" main.py --selftest
  goto :end

:run_dump
  "%PYTHON%" main.py --dump-report
  goto :end

:run_mcp
  "%PYTHON%" main.py --mcp-check
  goto :end

:run_task
  "%PYTHON%" main.py %*
  goto :end

:usage
  echo Usage: run.bat check^|selftest^|dump-report^|mcp-check^|setup^|"your task"
  goto :end

:fail
  echo.
  echo [setup] FAILED — see README.md 「环境搭建」 for the manual path.
  exit /b 1

:end
  endlocal
