@echo off
rem First start of a downloaded helper: finds (or installs) Python, prepares the helper once, then runs it.
title Helper setup
cd /d "%~dp0"
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo Python 3.10 or newer is needed and was not found on this PC. Installing it now ^(one time^)...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo Python could not be installed automatically.
    echo Please install it yourself: open https://www.python.org/downloads/ and download Python.
    echo While installing, tick the box "Add python.exe to PATH". Then double-click "Start helper" again.
    pause
    exit /b
  )
  echo.
  echo Python was installed. CLOSE this window and double-click "Start helper" again.
  pause
  exit /b
)
if not exist ".venv\ready.txt" (
  echo Preparing the helper ^(one time, a few minutes^)...
  if exist ".venv" rmdir /s /q ".venv"
  %PY% -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r worker\requirements.txt
  if errorlevel 1 goto failed
  echo ok> ".venv\ready.txt"
)
call worker\run_worker.bat
exit /b
:failed
echo.
echo Something could not be installed. Check the internet connection and double-click "Start helper" again.
pause
