@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [NSEBOT] Starting Streamlit dashboard on http://localhost:8501 ...
"%PYTHON_EXE%" -m streamlit run src\dashboard\app.py
pause
