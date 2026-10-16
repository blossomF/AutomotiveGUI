@echo off
REM Demo Day 机器（Windows）的一键入口。
REM 组委会会派人只照着 README 搭环境，所以
REM 本脚本必须不依赖 uv、不依赖 make、不依赖 cmd 以外的 shell 也能跑。
REM
REM   run.bat check                     设备 + 抓帧 + 输入自检
REM   run.bat dump-report               实测控件树质量 -> artifacts\dump-report.md
REM   run.bat selftest                  离线测试，不需要车机
REM   run.bat "在音乐App播放《七里香》"    跑一个任务

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
  echo.
  echo [Tip] Double-clicking runs no task. Re-run with an argument, e.g.:
  echo       run.bat selftest
  pause
  goto :end

:fail
  echo.
  echo [setup] FAILED — see ..\..\README.md  for the manual path.
  exit /b 1

:end
  endlocal
