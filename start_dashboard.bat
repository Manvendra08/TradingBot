@echo off
REM ── NSEBOT Launcher — Streamlit Dashboard ──────────────────────────────
REM Opens the localhost Streamlit dashboard on http://localhost:8501

cd /d "%~dp0"

echo [NSEBOT] Starting Streamlit dashboard on http://localhost:8501 ...
streamlit run src\dashboard\app.py
pause
