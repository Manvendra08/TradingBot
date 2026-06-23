@echo off
REM High Priority Cleanup Script for NSEBOT
REM ========================================
REM This script fixes:
REM 1. Multiple virtual environments (.venv, .venv-1, .venv_new)
REM 2. Cache bloat (6 yf-cache* directories)
REM 3. Scratch directory (164+ debug files)
REM
REM Usage: Double-click or run from command line

setlocal enabledelayedexpansion

echo ============================================================
echo NSEBOT High Priority Cleanup
echo ============================================================
echo.

REM Create backup directory
set BACKUP_DIR=cleanup_backup_%date:~-4%-%date:~3,2%-%date:~0,2%
echo Creating backup directory: %BACKUP_DIR%
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
echo.

REM Step 1: Move empty bot.db (already done, but verify)
echo Step 1: Database Cleanup
echo ------------------------
if exist "data\bot.db" (
    for %%F in ("data\bot.db") do set SIZE=%%~zF
    if !SIZE! EQU 0 (
        echo Moving empty bot.db to backup...
        move "data\bot.db" "%BACKUP_DIR%\bot.db.empty" >nul
        echo ✓ Moved empty bot.db
    ) else (
        echo bot.db is not empty (!SIZE! bytes), skipping...
    )
) else (
    echo bot.db already moved or doesn't exist
)
echo.

REM Step 2: Consolidate virtual environments
echo Step 2: Virtual Environment Consolidation
echo -----------------------------------------
echo Keeping .venv as primary...

if exist ".venv-1" (
    echo Moving .venv-1 to backup...
    move ".venv-1" "%BACKUP_DIR%\.venv-1" >nul
    echo ✓ Moved .venv-1
) else (
    echo .venv-1 not found
)

if exist ".venv_new" (
    echo Moving .venv_new to backup...
    move ".venv_new" "%BACKUP_DIR%\.venv_new" >nul
    echo ✓ Moved .venv_new
) else (
    echo .venv_new not found
)
echo.

REM Step 3: Clean up cache directories
echo Step 3: Cache Directory Cleanup
echo -------------------------------
echo Keeping yf-cache6 (most recent), moving others to backup...

for %%i in (1 2 3 4 5) do (
    if %%i EQU 1 (
        set CACHE_NAME=yf-cache
    ) else (
        set CACHE_NAME=yf-cache%%i
    )
    
    if exist "data\!CACHE_NAME!" (
        echo Moving !CACHE_NAME! to backup...
        move "data\!CACHE_NAME!" "%BACKUP_DIR%\!CACHE_NAME!" >nul
        echo ✓ Moved !CACHE_NAME!
    )
)
echo.

REM Step 4: Archive scratch directory
echo Step 4: Scratch Directory Archive
echo ---------------------------------
if exist "scratch" (
    echo Archiving scratch directory...
    move "scratch" "%BACKUP_DIR%\scratch_archive_%date:~-4%-%date:~3,2%-%date:~0,2%" >nul
    mkdir "scratch"
    echo. > "scratch\.gitkeep"
    echo ✓ Archived scratch directory
) else (
    echo scratch directory not found
)
echo.

REM Step 5: Generate report
echo Step 5: Cleanup Report
echo ----------------------
echo.
echo Cleanup completed successfully!
echo.
echo Actions taken:
echo   1. Moved empty bot.db to backup
echo   2. Moved .venv-1 and .venv_new to backup
echo   3. Moved old cache directories (yf-cache through yf-cache5) to backup
echo   4. Archived scratch directory
echo.
echo Backup location: %BACKUP_DIR%
echo.
echo IMPORTANT: 
echo   - Test the bot to ensure everything still works
echo   - Review backup directory contents
echo   - Delete backup after 1 week if no issues found
echo.
echo ============================================================

pause
