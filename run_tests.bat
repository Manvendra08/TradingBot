@echo off
REM ── NSEBOT — Run All Tests ─────────────────────────────────────────────
cd /d "%~dp0"
echo [NSEBOT] Running test suite...
pytest tests\ -v --tb=short
pause
