@echo off
rem Turns off the helper's auto-start and stops a running background helper.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClipStudioHelper.vbs" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_hidden.bat*') -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*clipstudio_worker.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Auto-start is OFF and the background helper was stopped.
pause
