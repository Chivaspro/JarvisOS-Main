@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║       J.A.R.V.I.S. OS  —  Dependency Installer      ║
echo  ║       Isolated virtual environment  (.venv\)         ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

rem ── 1. Find a usable Python 3.11+ interpreter ─────────────────────────────
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo  [ERROR] Python was not found on PATH.
  echo.
  echo  Option A — run setup_jarvis.py and choose [p] Install Python
  echo  Option B — download from https://python.org/downloads
  echo             and tick "Add Python to PATH" in the installer.
  echo.
  pause
  exit /b 1
)

rem ── 2. Check minimum version ───────────────────────────────────────────────
for /f "tokens=2 delims=." %%M in ('!PY! --version 2^>^&1') do set "PYMINOR=%%M"
if defined PYMINOR if !PYMINOR! LSS 11 (
  echo  [WARN] Python 3.!PYMINOR! found — J.A.R.V.I.S. needs 3.11 or newer.
  echo  Run setup_jarvis.py ^> [p] Install Python to upgrade automatically.
  echo.
)

echo  Using interpreter: 
!PY! --version
echo.

rem ── 3. Create the local virtual environment if it does not exist ──────────
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo  [..] Creating isolated virtual environment at .venv\ ...
  !PY! -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo  [ERROR] Could not create virtual environment.
    pause
    exit /b 1
  )
  echo  [ OK] Virtual environment created.
) else (
  echo  [ OK] Virtual environment already exists at .venv\
)
echo.

rem ── 4. Upgrade pip inside the venv ────────────────────────────────────────
echo  [..] Upgrading pip inside .venv\ ...
"%VENV_PY%" -m pip install --quiet --upgrade pip
echo  [ OK] pip is up to date.
echo.

rem ── 5. Install requirements.txt into the venv ─────────────────────────────
if not exist "%~dp0requirements.txt" (
  echo  [ERROR] requirements.txt not found in the project folder.
  pause
  exit /b 1
)

echo  [..] Installing packages from requirements.txt into .venv\ ...
echo       (this may take several minutes on first run)
echo.
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt" --disable-pip-version-check
if errorlevel 1 goto :failed

echo.
echo  ══════════════════════════════════════════════════════
echo   [ OK] All dependencies installed into .venv\
echo.
echo   To launch J.A.R.V.I.S.:   run_jarvis.bat
echo   To manage the setup:      python setup_jarvis.py
echo   To clean up (reset):      delete the .venv\ folder
echo  ══════════════════════════════════════════════════════
echo.
pause
exit /b 0

:failed
echo.
echo  ══════════════════════════════════════════════════════
echo   [FAIL] Dependency installation failed.
echo.
echo   Troubleshooting:
echo     • Check internet connection
echo     • Open setup_jarvis.py ^> [t] View logs  for details
echo     • Try:  python setup_jarvis.py ^> [v] Wipe .venv,
echo             then run this installer again
echo  ══════════════════════════════════════════════════════
echo.
pause
exit /b 1
