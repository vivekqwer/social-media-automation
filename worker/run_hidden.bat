@echo off
rem Started (hidden) at Windows sign-in by install_autostart.bat. Restarts the helper if it stops.
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:run
"%PY%" -u worker\clipstudio_worker.py >> worker\helper.log 2>&1
rem exit code 3 = this helper was removed in the dashboard: stop for good
if errorlevel 3 if not errorlevel 4 goto end
timeout /t 15 /nobreak >nul
goto run
:end
