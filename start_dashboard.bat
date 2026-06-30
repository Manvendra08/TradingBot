@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [NSEBOT] Starting FastAPI Dashboard Server on http://localhost:8080 ...
"%PYTHON_EXE%" dashboard_server.py
pause
