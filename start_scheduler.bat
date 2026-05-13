@echo off
REM ── NSEBOT Launcher — Scheduler ────────────────────────────────────────
REM Starts the 15-minute option chain monitor (blocks until Ctrl+C)
REM Place your .env file in the same folder as this script.

cd /d "%~dp0"

IF NOT EXIST ".env" (
    echo [WARN] .env not found. Make sure credentials are in system environment.
)

echo [NSEBOT] Starting scheduler...
python main.py
pause
