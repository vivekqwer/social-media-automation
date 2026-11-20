@echo off
title Clip Studio helper PC
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:run
"%PY%" worker\clipstudio_worker.py %*
rem exit code 4 = the helper updated itself: install any new requirements, then start it again
if errorlevel 4 if not errorlevel 5 (
  "%PY%" -m pip install -q -r worker\requirements.txt
  goto run
)
pause
