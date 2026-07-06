@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\manve\AppData\Local\Programs\Python\Python312\python.exe"

IF NOT EXIST ".env" (
    echo [WARN] .env not found. Make sure credentials are in system environment.
)

echo [NSEBOT] Starting scheduler...
"%PYTHON_EXE%" main.py %*
pause
