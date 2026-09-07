@echo off
title ATIP - AI Trading Intelligence Platform
color 0A

echo.
echo  ================================================
echo    ATIP - AI Trading Intelligence Platform
echo  ================================================
echo.

REM Use this script's own folder, NOT a hardcoded path. The previous version did
REM `cd /d D:\Projects\atip`, which breaks silently the moment the project is
REM moved or renamed - and ATIP_TaskScheduler.xml points at the same fixed path.
REM %~dp0 is the directory this .bat lives in, so it works wherever it sits.
cd /d "%~dp0"

echo  Project    : %CD%
echo  Dashboard  : http://localhost:8000
echo  Press Ctrl+C to stop ATIP (this also stops the order monitor).
echo.

REM Fail loudly if python isn't on PATH, instead of flashing a window shut.
where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] python is not on PATH. Install Python or fix PATH, then re-run.
    pause
    exit /b 1
)

REM Open the dashboard once the server has had time to bind.
start /B cmd /C "timeout /t 10 /nobreak >nul && start http://localhost:8000"

python main.py

REM Only reached if ATIP exits or crashes - keep the window so the error is readable.
echo.
echo  ATIP has stopped. Review the messages above and atip_data\atip.log
pause
