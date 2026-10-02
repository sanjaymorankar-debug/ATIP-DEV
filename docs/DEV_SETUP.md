# ATIP — development environment

A repeatable local setup for developing and testing ATIP. ATIP runs on one
Windows machine and is never exposed to the internet; nothing here deploys it.

## 1. Python

Python 3.11 or newer (the live install runs 3.14). Use a virtual environment:

```bat
cd /Users/agtci/Documents/Project_Documents/Projects/ATIP
python -m venv .venv
.venv\Scripts\activate
```

## 2. Packages

```bat
pip install -r requirements-dev.txt
```

`requirements-dev.txt` is `requirements.txt` plus `pytest` and `httpx` (FastAPI's
`TestClient` needs it). Technical indicators come from **`ta`** — do not install
`pandas-ta`: it imports `numpy.NaN` (removed in numpy 2) and needs `numba`, which
has no Python 3.14 wheel. `kiteconnect` is only needed for a Zerodha portfolio
(`pip install -e .[zerodha]`).

`install.bat`, `pip_install.bat` and `setup_atip.ps1` install the same set.

## 3. Configuration

```bat
copy config_template.json atip_data\config.json
```

Fill in what you use; everything else can stay a placeholder:

| Key | Needed for |
|---|---|
| `dhan_client_id`, `dhan_access_token` | market data, portfolio sync, orders. The token expires — a `DH-901` error means regenerate it at web.dhan.co |
| `telegram_token`, `telegram_chat_id` | Telegram alerts (`python -m alerts.telegram --test` explains setup). Without them alerts still appear on the dashboard |
| `ANTHROPIC_API_KEY` (environment variable) | AI news classification; without it the rule-based fallback runs |
| `risk_limits` | pre-trade limits; each is enforced only when set (orders/risk.py) |
| `position_sizing` | risk-based sizing defaults (orders/risk.py) |
| `broker_env` | `PAPER` (default), `SANDBOX` or `LIVE` |

## 4. Database

```bat
python main.py --init
```

Creates `atip_data/atip.db`. Every later schema change is an additive migration
applied on connection (`db/schema.py get_connection`), so an existing database
upgrades itself; nothing is dropped.

## 5. Running

| Command | What |
|---|---|
| `python main.py` | scheduler + dashboard (http://localhost:8000) — the normal mode |
| `python main.py --run postmarket` | one post-market run now |
| `python -m pipeline.health` | which scheduled jobs are missed, stalled, failing or empty |
| `python -m data.quality --date YYYY-MM-DD` | data-quality checks for one session |
| `python -m portfolio.pnl` / `--record` | paper and live books; store today's daily P&L |
| `python -m orders.risk --status` | kill switch, limits, sizing, today's P&L and drawdown |

Do not restart the live instance with `--dashboard`: that runs the web server
only, and the scheduler's evening jobs silently stop.

## 6. Tests

```bat
python -m pytest
```

Tests never touch `atip_data/atip.db` or Telegram: `tests/conftest.py` gives every
test its own database and stubs Telegram delivery.

Logs: `atip_data/atip.log`, rotated at 20 MB, 10 files kept.
