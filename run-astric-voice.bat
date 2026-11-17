@echo off
setlocal EnableDelayedExpansion
title Astric Voice launcher
REM ============================================================
REM  Astric Voice backend - one-click start for Windows
REM  Put this file in the astric-voice-backend folder (next to
REM  the "app" folder and requirements.txt), then double-click.
REM  Starts: Redis (via Docker) + API + Celery worker + Celery beat
REM ============================================================
cd /d "%~dp0"

REM ---- 1. .env must exist --------------------------------------------------
if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo.
    echo  [!] A new .env was created from .env.example.
    echo      Fill in PUBLIC_BASE_URL, FIREBASE_*, SECRETS_KEY, CALL_TOKEN_SECRET,
    echo      DEEPGRAM_API_KEY, SARVAM_API_KEY, GROQ_API_KEY - then run this again.
    notepad ".env"
  ) else (
    echo  [X] .env and .env.example not found. Run this from the astric-voice-backend folder.
  )
  pause
  exit /b 1
)

REM ---- 2. Find Python (3.11 / 3.12 recommended) ---------------------------
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY ( py -3.11 --version >nul 2>&1 && set "PY=py -3.11" )
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY (
  echo  [X] Python not found. Install Python 3.12 from python.org ^(tick "Add to PATH"^).
  pause
  exit /b 1
)
echo  Using: %PY%

REM ---- 3. Virtual environment: use it if it exists, create it if not ------
set "VPY=%~dp0.venv\Scripts\python.exe"
if exist "%VPY%" (
  "%VPY%" --version >nul 2>&1
  if errorlevel 1 (
    echo  [!] The existing .venv is broken - recreating it...
    rmdir /s /q ".venv"
  )
)
if exist "%VPY%" (
  echo  Virtual environment found: .venv
) else (
  echo  Virtual environment not found - creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 ( echo  [X] Could not create the virtual environment. & pause & exit /b 1 )
)

REM ---- 4. requirements.txt: install if missing or changed ------------------
REM  Installs when: packages are not importable, or requirements.txt changed
REM  since the last install.
set "NEWHASH="
for /f "skip=1 tokens=* delims=" %%H in ('certutil -hashfile requirements.txt SHA256') do if not defined NEWHASH set "NEWHASH=%%H"
set "NEWHASH=!NEWHASH: =!"
set "OLDHASH="
if exist ".venv\.req_hash" set /p OLDHASH=<".venv\.req_hash"

set "NEEDINSTALL=0"
"%VPY%" -c "import fastapi, uvicorn, celery, redis, pipecat" >nul 2>&1
if errorlevel 1 set "NEEDINSTALL=1"
if not "!NEWHASH!"=="!OLDHASH!" set "NEEDINSTALL=1"

if "!NEEDINSTALL!"=="1" (
  echo  Installing requirements - this takes a few minutes the first time...
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 ( echo  [X] pip install failed. Scroll up for the error. & pause & exit /b 1 )
  >".venv\.req_hash" echo !NEWHASH!
) else (
  echo  Requirements already installed and up to date.
)

REM ---- 5. Redis ---------------------------------------------------------
"%VPY%" -c "import socket;s=socket.socket();s.settimeout(1);s.connect(('127.0.0.1',6379))" >nul 2>&1
if errorlevel 1 (
  echo  Redis is not running on port 6379. Trying Docker...
  docker --version >nul 2>&1
  if errorlevel 1 (
    echo  [X] Docker not found. Install Docker Desktop ^(or run Redis/Memurai on port 6379^), then try again.
    pause
    exit /b 1
  )
  docker start astric-redis >nul 2>&1
  if errorlevel 1 (
    docker run -d --name astric-redis -p 6379:6379 --restart unless-stopped redis:7-alpine redis-server --appendonly yes
    if errorlevel 1 ( echo  [X] Could not start Redis in Docker. Is Docker Desktop running? & pause & exit /b 1 )
  )
  timeout /t 3 /nobreak >nul
)
echo  Redis OK.

REM ---- 6. Start the three processes in their own windows -------------------
set "REDIS_URL=redis://localhost:6379/0"
set "PYTHONUNBUFFERED=1"

start "Astric Voice - API (port 8000)" cmd /k ""%VPY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"
start "Astric Voice - Worker" cmd /k ""%VPY%" -m celery -A app.celery_app worker --loglevel=INFO --pool=solo"
start "Astric Voice - Beat" cmd /k ""%VPY%" -m celery -A app.celery_app beat --loglevel=INFO"

echo.
echo  Started. API: http://localhost:8000/v1/health
echo.
echo  REMINDER: carriers (Vobiz / Telnyx) must reach this server over HTTPS.
echo  For testing from your PC, expose port 8000 with a tunnel, e.g.
echo      cloudflared tunnel --url http://localhost:8000
echo  then put that https address in PUBLIC_BASE_URL in .env and restart.
echo  To stop everything: close the three windows.
echo.
pause