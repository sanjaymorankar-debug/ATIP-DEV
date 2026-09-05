@echo off
title ATIP - AI Trading Intelligence Platform
color 0A

echo.
echo  ================================================
echo    ATIP - AI Trading Intelligence Platform
echo    Starting up...
echo  ================================================
echo.

cd /d D:\Projects\atip

echo  Starting ATIP scheduler + dashboard...
echo  Dashboard will open at: http://localhost:8000
echo  Press Ctrl+C to stop ATIP
echo.

REM Open browser after 8 seconds
start /B cmd /C "timeout /t 8 /nobreak >nul && start http://localhost:8000"

REM Start ATIP
python main.py

pause
