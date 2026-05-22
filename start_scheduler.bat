@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

IF NOT EXIST ".env" (
    echo [WARN] .env not found. Make sure credentials are in system environment.
)

echo [NSEBOT] Starting scheduler...
"%PYTHON_EXE%" main.py
pause
