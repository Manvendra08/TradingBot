@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [NSEBOT] Running one-shot pipeline for all symbols...
"%PYTHON_EXE%" main.py --now
echo.
echo Done. Check logs\nsebot-main.log for details.
pause
