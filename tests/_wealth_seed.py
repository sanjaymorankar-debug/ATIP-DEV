"""
Deterministic synthetic market + investor for the W11-W17 wealth tests.

seed_market(conn): 260 weekday sessions ending on the last weekday before today of
    NIFTY50 (steady +0.03%/day with a mild wave), ACME (+0.05%/day), GOLDBEES and
    LIQUIDBEES, plus market_health, ai_scores and global_markets rows so the tactical
    signals have data. No randomness: every run sees the same numbers.
ANSWERS: a complete, valid Investor DNA questionnaire.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

OWNER = {"tenant_id": "default", "owner_id": "owner"}
OTHER = {"tenant_id": "t_other", "owner_id": "u_other"}

ANSWERS = {"age": 38, "dependents": 2, "employment": "SALARIED_PRIVATE", "monthly_income": 200000,
           "monthly_expenses": 90000, "monthly_emi": 30000, "emergency_fund_months": 6, "horizon_years": 15,
           "liquidity_need": "NONE", "drop20": "HOLD", "tradeoff": "C", "objective": "GROWTH", "loss_comfort": "CALM",
           "max_annual_loss": "20", "target_return": "13", "experience_years": "3", "products": "ACTIVE",
           "knowledge": "INTERMEDIATE", "trade_frequency": "MONTHLY", "holding_period": "MONTHS",
           "check_frequency": "DAILY", "fomo": "RESEARCH", "loser": "REVIEW", "winner": "TRIM",
           "confidence": "NEUTRAL", "after_good_year": "STAY", "coin_flip": "20000", "mode": "BOTH"}


def sessions(n=260, end=None):
    d = end or date.today() - timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def seed_market(conn, n=260):
    ds = sessions(n)
    series = {"NIFTY50": (20000.0, 0.0003), "ACME": (100.0, 0.0005), "GOLDBEES": (60.0, 0.0002),
              "LIQUIDBEES": (1000.0, 0.0), "NIFTYMIDCAP150": (18000.0, 0.0004)}
    for sym, (p0, g) in series.items():
        for i, d in enumerate(ds):
            c = p0 * (1 + g) ** i * (1 + 0.01 * math.sin(i / 9))
            conn.execute("INSERT OR REPLACE INTO prices_daily (symbol,date,open,high,low,close,volume) "
                         "VALUES (?,?,?,?,?,?,?)", (sym, str(d), c * 0.999, c * 1.01, c * 0.99, c, 1_000_000))
    last = ds[-1]
    conn.execute("INSERT INTO market_health (date,mh_score,regime,breadth,fii_score,global_score) VALUES "
                 "(?,?,?,?,?,?)", (str(last), 62.0, "BULL", 55.0, 60.0, 58.0))
    conn.execute("INSERT INTO ai_scores (symbol,date,atip_score,vpi,cri,zpi,signal,regime) VALUES "
                 "(?,?,?,?,?,?,?,?)", ("ACME", str(last), 72.0, 65.0, 30.0, 60.0, "BUY", "BULL"))
    for i, d in enumerate(ds[-30:]):
        conn.execute("INSERT INTO global_markets (date,time,gold,usd_inr,global_score) VALUES (?,?,?,?,?)",
                     (str(d), "overnight", 4000 + i * 5, 95.0 + i * 0.01, 58.0))
    conn.commit()
    return ds


def fresh(temp_db):
    """init_db() on the per-test database, market seeded; returns (conn, sessions)."""
    from db.schema import get_connection, init_db
    init_db()
    conn = get_connection()
    return conn, seed_market(conn)
