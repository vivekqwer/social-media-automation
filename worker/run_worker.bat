@echo off
title Clip Studio helper PC
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:run
"%PY%" worker\clipstudio_worker.py %*
rem exit code 4 = the helper updated itself, start it again
if errorlevel 4 if not errorlevel 5 goto run
pause
