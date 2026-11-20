@echo off
rem Turns off the helper's auto-start and stops a running background helper.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClipStudioHelper.vbs" 2>nul
wmic process where "name='cmd.exe' and commandline like '%%run_hidden.bat%%'" call terminate >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%clipstudio_worker.py%%'" call terminate >nul 2>&1
echo Auto-start is OFF and the background helper was stopped.
pause
