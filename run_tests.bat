@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [NSEBOT] Running test suite...
"%PYTHON_EXE%" -m pytest tests -v --tb=short
pause
