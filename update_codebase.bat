@echo off
echo ===================================================
echo Syncing NSEBOT src and config to Codebase Backup...
echo ===================================================

echo Syncing src/ ...
robocopy "C:\Users\manve\Downloads\NSEBOT\src" "C:\Users\manve\Downloads\NSEBOT\Codebase\src" /E /XO /FFT /R:2 /W:2

echo Syncing config/ ...
robocopy "C:\Users\manve\Downloads\NSEBOT\config" "C:\Users\manve\Downloads\NSEBOT\Codebase\config" /E /XO /FFT /R:2 /W:2

echo ===================================================
echo Sync completed!
echo ===================================================
pause
