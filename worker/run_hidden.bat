@echo off
rem Started (hidden) at Windows sign-in by install_autostart.bat. Restarts the helper if it stops.
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:run
rem keep the log small: start a new one above 5 MB
for %%A in ("worker\helper.log") do if %%~zA GTR 5000000 move /y "worker\helper.log" "worker\helper.old.log" >nul
"%PY%" -u worker\clipstudio_worker.py >> worker\helper.log 2>&1
rem 9009 = Python not found, 5 = another helper already runs: stop instead of retrying forever
if errorlevel 9009 goto end
if errorlevel 5 if not errorlevel 6 goto end
rem 4 = the helper updated itself: install any new requirements first
if errorlevel 4 if not errorlevel 5 "%PY%" -m pip install -q -r worker\requirements.txt >> worker\helper.log 2>&1
timeout /t 15 /nobreak >nul
goto run
:end
