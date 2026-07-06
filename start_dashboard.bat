@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\manve\AppData\Local\Programs\Python\Python312\python.exe"

echo [NSEBOT] Starting FastAPI Dashboard Server on http://localhost:8080 ...
"%PYTHON_EXE%" dashboard_server.py
pause
