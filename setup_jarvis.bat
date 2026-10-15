@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title J.A.R.V.I.S  //  SETUP & CONTROL CENTER
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
color 0B
cls

rem == J.A.R.V.I.S setup launcher ============================================
rem Same look as run_jarvis.bat: UTF-8 console, cyan-on-black, gradient banner.
rem setup_jarvis.py is stdlib-only, so ANY Python 3.11+ can run it.

set "SCRIPT=%~dp0setup_jarvis_modern.py"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0setup_jarvis.py"

set "PY="
where python >nul 2>&1
if not errorlevel 1 (
  python -c "import PyQt6" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import PyQt6" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
  )
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :nopython

%PY% "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
exit /b 0

:nopython
echo.
echo   [ X ]  Python was not found.
echo.
echo          Install Python 3.11+ and enable "Add Python to PATH",
echo          then run this again.
echo.
pause
exit /b 1

:failed
color 0C
echo.
echo   [ X ]  J.A.R.V.I.S Setup exited with code %RC%
echo          Script : %SCRIPT%
echo.
pause
exit /b %RC%