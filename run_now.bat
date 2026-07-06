@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\manve\AppData\Local\Programs\Python\Python312\python.exe"

echo [NSEBOT] Running one-shot pipeline for all symbols...
"%PYTHON_EXE%" main.py --once
echo.
echo Done. Check logs\nsebot-main.log for details.
pause
