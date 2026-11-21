@echo off
setlocal
cd /d "%~dp0"
python launcher.py host %*
if errorlevel 1 pause
