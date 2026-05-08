@echo off
REM One-command local launcher for the Reddit Sales POC (Windows).
REM
REM   run.bat                 set up + start on http://localhost:8000
REM   set PORT=9000 ^&^& run.bat   custom port
REM
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"

if "%PORT%"=="" set PORT=8000

REM --- 1. Find Python ------------------------------------------------------
set "PYTHON="
for %%P in (py python python3) do (
  if not defined PYTHON (
    where %%P >nul 2>nul
    if !errorlevel! equ 0 (
      %%P -c "import sys; assert sys.version_info >= (3,9)" >nul 2>nul
      if !errorlevel! equ 0 set "PYTHON=%%P"
    )
  )
)
if not defined PYTHON (
  echo [x] Python 3.9+ is required. Install from https://www.python.org/downloads/ and re-run.
  exit /b 1
)
echo [+] Using Python: %PYTHON%

REM --- 2. Create venv ------------------------------------------------------
if not exist "%ROOT%.venv" (
  echo [*] Creating virtualenv in .venv
  %PYTHON% -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo [x] Could not create virtualenv.
    exit /b 1
  )
)
call "%ROOT%.venv\Scripts\activate.bat"

REM --- 3. Install deps -----------------------------------------------------
echo [*] Installing dependencies
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "%ROOT%backend\requirements.txt"
if errorlevel 1 (
  echo [x] pip install failed.
  exit /b 1
)

REM --- 4. .env -------------------------------------------------------------
if not exist "%ROOT%backend\.env" (
  copy /Y "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
  echo [!] Created backend\.env from the example. Edit it and paste your OPENAI_API_KEY.
)

REM --- 5. Start ------------------------------------------------------------
echo [*] Starting server on http://localhost:%PORT%
start "" "http://localhost:%PORT%"
cd /d "%ROOT%backend"
python main.py
