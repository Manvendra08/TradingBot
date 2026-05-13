@echo off
REM Explicit launcher for the Chrome extension bridge on localhost:8765

cd /d "%~dp0"

IF NOT EXIST ".env" (
    echo [WARN] .env not found. Make sure credentials are in system environment.
)

echo [NSEBOT] Starting extension bridge on http://127.0.0.1:8765 ...
python main.py --bridge
pause
