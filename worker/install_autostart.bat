@echo off
rem Makes the Clip Studio helper start by itself (hidden) every time you sign in to Windows.
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClipStudioHelper.vbs"
> "%VBS%" echo CreateObject("WScript.Shell").Run """%~dp0run_hidden.bat""", 0, False
echo.
echo  Auto-start is ON. The helper now runs in the background whenever this PC is on.
echo  Log file: %~dp0helper.log
echo  To turn it off, run uninstall_autostart.bat
echo.
wscript "%VBS%"
pause
