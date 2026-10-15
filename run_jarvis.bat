@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title J.A.R.V.I.S  //  LAUNCH SEQUENCE
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
color 0B
cls

rem == J.A.R.V.I.S launcher ==================================================
rem This script deliberately does NOT decide which Python runs the app.
rem It hands over to start_jarvis.py, which is stdlib-only (so ANY interpreter
rem below can run it). start_jarvis.py then:
rem
rem   1. OPTIMIZES before launch - clears leftover cache/temp, prunes orphaned
rem      bytecode, trims oversized logs, compacts the chat database
rem   2. probes every Python on this machine and picks the first one that can
rem      really import the project's packages (installing only if none can)
rem   3. starts JARVIS
rem
rem ...while drawing the animated launch screen (banner, gradient progress bar,
rem live percentage, decoding status text and a scrolling log feed).
rem The window is kept simple on purpose: no prompts, nothing to click.

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

%PY% "%~dp0start_jarvis.py" --console-ui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
exit /b 0

:nopython
echo.
echo   [ X ]  Python was not found.
echo.
echo          Install Python 3.11+ and enable "Add Python to PATH", then
echo          run install_dependencies.bat once and start JARVIS again.
echo.
pause
exit /b 1

:failed
color 0C
echo.
echo   [ X ]  J.A.R.V.I.S did not start cleanly  (exit code %RC%)
echo.
echo          Startup error : jarvis_startup_error.log
echo          Install log   : data\logs\dependency_install.log
echo.
pause
exit /b %RC%