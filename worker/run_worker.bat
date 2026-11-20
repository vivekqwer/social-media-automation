@echo off
title Clip Studio helper PC
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" worker\clipstudio_worker.py %*
) else (
  python worker\clipstudio_worker.py %*
)
pause
