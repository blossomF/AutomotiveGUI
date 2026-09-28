@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
"server\autogui\.venv\Scripts\python.exe" -u "dist\dashboard_server.py" 1>".dashboard.log" 2>".dashboard.log.err"
