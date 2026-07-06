@echo off
title NSEBOT Launcher
color 0A
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\manve\AppData\Local\Programs\Python\Python312\python.exe"

echo.
echo  =========================================
echo   NSEBOT v2.0  ^|  Chrome-Free Mode
echo  =========================================
echo.

IF NOT EXIST ".env" (
    echo  [WARN] .env not found - credentials must be in system environment
    echo.
)

%PYTHON_EXE% --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo  [ERROR] Python not found. Install Python 3.12+ or recreate .venv and retry.
    pause & exit /b 1
)

echo  [1/3] Checking dependencies...
%PYTHON_EXE% -c "import fastapi, uvicorn, apscheduler, yfinance, pyotp" >nul 2>&1
IF ERRORLEVEL 1 (
    echo  [INFO] Installing missing packages...
    %PYTHON_EXE% -m pip uninstall python-telegram-bot -y >nul 2>&1
    %PYTHON_EXE% -m pip install fastapi uvicorn apscheduler yfinance python-dotenv requests pytz dhanhq pyotp python-telegram-bot==21.5 --quiet
    IF ERRORLEVEL 1 (
        echo  [ERROR] pip install failed.
        pause & exit /b 1
    )
)
echo        OK

echo  [2/3] Initialising database...
%PYTHON_EXE% -c "from src.models.schema import init_db; init_db()"
IF ERRORLEVEL 1 (
    echo  [ERROR] DB init failed. Check logs.
    pause & exit /b 1
)
echo        OK

echo  [3/3] Starting services...
echo.

start "NSEBOT Scheduler" /min cmd /c ""%PYTHON_EXE%" main.py 2>> logs\nsebot-main.log"

timeout /t 3 /nobreak >nul

start "NSEBOT Dashboard" cmd /c ""%PYTHON_EXE%" dashboard_server.py"

for /l %%i in (1,1,20) do (
    powershell -NoProfile -Command "$c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 8080); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 goto dashboard_ready
    timeout /t 1 /nobreak >nul
)

:dashboard_ready
start http://localhost:8080

echo  Scheduler  ^| running in background  ^| logs\nsebot-main.log
echo  Dashboard  ^| http://localhost:8080
echo.
echo  Close this window safely - services run independently.
echo  To stop: close the Scheduler and Dashboard windows.
echo.
pause
