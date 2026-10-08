"""W39b owner notes: the trading-style stock chart (DB-20).

The 7-year history backfill and the Dhan token renewal are W39's (PR #4: data/history_backfill.py,
the LaunchAgent / Windows task running tools/dhan_token_refresh.py), tested in test_w39_history.py
and test_dhan_token_refresh.py; W39b's own versions were dropped on merging #4."""

import json
from datetime import date, timedelta

import pytest


def _db():
    from db.schema import get_connection, init_db
    init_db()
    return get_connection()


def _bars(conn, sym, start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "(?,?,?,?,?,?,?)", (sym, str(d), 100, 101, 99, 100, 1000))
        d += timedelta(days=1)
    conn.commit()


def test_config_template_documents_the_token_renewal_secrets_blank():
    from pathlib import Path
    t = json.loads((Path(__file__).resolve().parents[1] / "config_template.json").read_text(encoding="utf-8"))
    assert t["dhan_pin"] == "" and t["dhan_totp_secret"] == ""              # never a real secret in git
    for gone in ("dhan_token_auto_refresh", "dhan_token_refresh_time", "history_years", "history_backfill_weekly"):
        assert gone not in t, f"{gone}: W39b's duplicate switches were removed on merging #4"


# ── DB-20 stock history ────────────────────────────────────────────────

def test_stock_history_reports_multi_year_returns(temp_db):
    from dashboard.stock_view import stock_history
    conn = _db()
    d0 = date.today() - timedelta(days=3700)
    i, d = 0, d0
    while d < date.today():
        if d.weekday() < 5:
            conn.execute("INSERT INTO prices_daily (symbol,date,open,high,low,close,volume) VALUES "
                         "('ACME',?,?,?,?,?,1000)", (str(d), 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1))
            i += 1
        d += timedelta(days=1)
    conn.commit()
    h = stock_history(conn, "ACME", 2500)
    s = h["stats"]
    closes = [p["close"] for p in h["prices"]]
    assert len(closes) == 2500
    assert s["ret_5y"] == pytest.approx(round((closes[-1] / closes[-1 - 1250] - 1) * 100, 2))
    assert s["ret_all"] == pytest.approx(round((closes[-1] / closes[0] - 1) * 100, 2))
    assert s["first_date"] == h["prices"][0]["date"] and s["high_all"] >= s["high_52w"]
