@echo off
echo ============================================================
echo   ATIP - Installing all required packages
echo   Python version:
echo ============================================================
python --version
echo.

echo [1/8] Core packages...
pip install pandas numpy requests python-dotenv
if %ERRORLEVEL% NEQ 0 echo WARNING: Some core packages failed

echo.
echo [2/8] Dhan API (primary data source)...
pip install dhanhq
if %ERRORLEVEL% NEQ 0 echo WARNING: dhanhq install failed

echo.
echo [3/8] yfinance (global markets)...
pip install yfinance
if %ERRORLEVEL% NEQ 0 echo WARNING: yfinance install failed

echo.
echo [4/8] Technical indicators...
pip install pandas-ta --no-deps
pip install ta
echo NOTE: pandas-ta --no-deps skips numba, works on Python 3.14

echo.
echo [5/8] Scheduler...
pip install schedule

echo.
echo [6/8] Web dashboard...
pip install fastapi uvicorn[standard]

echo.
echo [7/8] AI and news packages...
pip install anthropic feedparser beautifulsoup4

echo.
echo [8/8] Utilities...
pip install tqdm openpyxl

echo.
echo ============================================================
echo   Installation complete!
echo.
echo   NEXT STEPS:
echo   1. mkdir atip_data
echo   2. copy config_template.json atip_data\config.json
echo   3. Edit atip_data\config.json - add Dhan credentials
echo   4. python main.py --init
echo   5. python dhan_test.py
echo   6. python main.py --dashboard
echo ============================================================
pause
