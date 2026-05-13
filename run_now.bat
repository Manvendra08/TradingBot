@echo off
REM ── NSEBOT Launcher — Manual / Test Run ───────────────────────────────
REM Runs the full pipeline once for all configured symbols, then exits.
REM Useful for testing credentials and data pipeline without waiting for 09:15.

cd /d "%~dp0"

echo [NSEBOT] Running one-shot pipeline for all symbols...
python main.py --now
echo.
echo Done. Check logs\nsebot-main.log for details.
pause
