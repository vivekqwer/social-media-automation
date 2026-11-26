@echo off
rem Started (hidden) at Windows sign-in. Makes the remaining parked clips into D:\youtube; finished ones are skipped,
rem so when everything is made it simply exits within a few seconds.
cd /d "D:\youtube\tool"
rem wait for the network after sign-in
timeout /t 90 /nobreak >nul
"D:\social medi engin\.venv\Scripts\python.exe" "D:\youtube\tool\make_local.py" >> "D:\youtube\tool\startup.log" 2>&1
