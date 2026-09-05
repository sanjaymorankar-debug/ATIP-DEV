from setuptools import setup, find_packages

setup(
    name="atip",
    version="0.2",
    packages=find_packages(),
    install_requires=[
        "pandas", "numpy", "yfinance", "pandas-ta",
        "requests", "beautifulsoup4", "feedparser",
        "schedule", "fastapi", "uvicorn", "anthropic",
        "python-dotenv", "tqdm", "openpyxl",
    ],
)
