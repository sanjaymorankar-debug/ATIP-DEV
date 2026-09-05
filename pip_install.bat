@echo off
echo Running pip install for all ATIP packages...
echo.
pip install pandas numpy requests python-dotenv dhanhq yfinance schedule fastapi "uvicorn[standard]" anthropic feedparser beautifulsoup4 tqdm openpyxl pandas-ta --no-deps
echo.
echo Done. Now run: python main.py --init
pause
