from setuptools import setup, find_packages

# Mirrors requirements.txt. `ta`, not pandas-ta: pandas-ta cannot install on
# this stack (it imports numpy.NaN, removed in numpy 2, and needs numba, which
# has no Python 3.14 wheel), so listing it made `pip install .` fail and a fresh
# install computed no indicators at all.
setup(
    name="atip",
    version="0.2",
    packages=find_packages(exclude=("tests",)),
    python_requires=">=3.11",
    install_requires=[
        "pandas>=2.0.0", "numpy>=1.24.0", "requests>=2.31.0",
        "dhanhq>=1.4.0", "yfinance>=0.2.38", "ta>=0.11.0",
        "schedule>=1.2.0", "fastapi>=0.110.0", "uvicorn[standard]>=0.29.0",
        "anthropic>=0.25.0", "feedparser>=6.0.11", "beautifulsoup4>=4.12.0",
        "python-dotenv>=1.0.0", "tqdm>=4.66.0", "openpyxl>=3.1.0",
    ],
    extras_require={
        "zerodha": ["kiteconnect>=4.2.0"],
        "postgres": ["psycopg[binary]>=3.2"],      # the server database (db/postgres.py)
        "dev": ["pytest>=8.0", "httpx>=0.27"],
    },
)
