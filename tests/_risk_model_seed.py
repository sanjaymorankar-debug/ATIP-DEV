"""
A deterministic synthetic market for the W40 factor risk model tests (tests/test_w40_risk_model.py).

seed(conn) writes, on a fixed weekday calendar ending 2026-09-30:
  NIFTY50          the benchmark (market factor m_t)
  53 stocks        4 NSE industries x 12 (Financial Services, Information Technology, Healthcare,
                   Capital Goods), 3 Textiles (a small industry: pooled into "Other") and 2 with no
                   NSE industry (also "Other"); daily return = beta_i m_t + g_{industry,t} + e_i
  fundamental_data quarterly filings (period end + 40 days = available_from) with shares_out,
                   book_value_ps, eps_ttm and roe for all but 6 stocks; LATEFIL's first filing is
                   only available from LATE_FILED, so before it LATEFIL has no market cap
  the PAPER book   5 positions and a cash balance
Returns a dict with the calendar, symbols, sectors and the model config the tests use.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

END = date(2026, 9, 30)
N_SESSIONS = 330
SECTORS = {}
for _sec, _pre in (("Financial Services", "FIN"), ("Information Technology", "TEC"), ("Healthcare", "HLT"),
                   ("Capital Goods", "CAP")):
    for _k in range(12):
        SECTORS[f"{_pre}{_k:02d}"] = _sec
for _k in range(3):
    SECTORS[f"TEX{_k:02d}"] = "Textiles"
UNCLASSIFIED = ["ZZZ00", "ZZZ01"]
LATE = "FIN11"                                  # files late: no market cap before LATE_FILED
NO_FUND = ["CAP10", "CAP11", "TEX02", "ZZZ01", "HLT11"]
SYMBOLS = sorted(list(SECTORS) + UNCLASSIFIED)
BOOK = {"FIN00": 120, "TEC03": 300, "HLT05": 80, "CAP02": 150, "TEX01": 400}

CFG = {"universe": SYMBOLS, "min_history": 60, "beta_window": 120, "beta_min_obs": 60, "beta_halflife": 40,
       "momentum_window": 120, "momentum_skip": 10, "liquidity_window": 20, "history_sessions": 400,
       "cov_min_obs": 30, "specific_min_obs": 15, "specific_window": 200, "bias_window": 150,
       "min_estimation_stocks": 20, "benchmark_names": 15, "benchmark_min_names": 10, "min_industry_size": 8,
       "vol_halflife": 60, "corr_halflife": 90, "cov_window": 400, "bias_random_portfolios": 4,
       "bias_random_size": 8, "shrinkage_buckets": 3}


def calendar(n=N_SESSIONS, end=END):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def cfg(**over):
    from quant.risk_model import validate
    c = validate({**CFG, **over})
    assert "_warnings" not in c, c.get("_warnings")
    return c


def seed(conn, n=N_SESSIONS, seed_=40) -> dict:
    rng = np.random.default_rng(seed_)
    cal = calendar(n)
    T = len(cal)
    m = rng.normal(0.0003, 0.01, T)
    m[0] = 0.0
    secs = sorted(set(SECTORS.values()))
    g = {s: rng.normal(0, 0.005, T) for s in secs}
    nifty = 20000 * np.cumprod(1 + m)
    rows = [("NIFTY50", str(d), c, c, c, c, 0, "dhan_index") for d, c in zip(cal, nifty)]
    info = {}
    for s in SYMBOLS:
        beta = rng.uniform(0.6, 1.4)
        sig = rng.uniform(0.008, 0.02)
        r = beta * m + (g[SECTORS[s]] if s in SECTORS else 0.0) + rng.normal(0, sig, T)
        r[0] = 0.0
        px = rng.uniform(50, 500) * np.cumprod(1 + r)
        vol = np.exp(rng.normal(13, 0.4, T)).round()
        start = 0 if s != "ZZZ00" else 150          # ZZZ00 lists late: no exposures before min_history
        rows += [(s, str(cal[i]), px[i], px[i] * 1.01, px[i] * 0.99, px[i], int(vol[i]), "bhavcopy")
                 for i in range(start, T)]
        info[s] = {"beta": beta, "sigma": sig, "shares": float(rng.uniform(1e7, 1e9))}
    conn.executemany("INSERT OR REPLACE INTO prices_daily (symbol, date, open, high, low, close, volume, source) "
                     "VALUES (?,?,?,?,?,?,?,?)", rows)
    frows = []
    quarters = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31),
                date(2026, 6, 30)]
    for s in SYMBOLS:
        if s in NO_FUND:
            continue
        sh = info[s]["shares"]
        for k, q in enumerate(quarters):
            if s == LATE and q < date(2026, 3, 31):
                continue
            avail = q + timedelta(days=40)
            bv = rng.uniform(20, 300)
            frows.append((s, f"Q{k}-{q.year}", str(q), str(q), f"{avail} 18:00:00", sh, bv, rng.uniform(-5, 40),
                          rng.uniform(-0.05, 0.3)))
    conn.executemany("INSERT INTO fundamental_data (symbol, quarter, report_date, period_end, available_from, shares_out, "
                     "book_value_ps, eps_ttm, roe) VALUES (?,?,?,?,?,?,?,?,?)", frows)
    from orders.paper import ensure_tables
    ensure_tables(conn)
    conn.executemany("INSERT OR REPLACE INTO paper_position (symbol, quantity, avg_price) VALUES (?,?,?)",
                     [(s, q, 100.0) for s, q in BOOK.items()])
    conn.execute("INSERT OR REPLACE INTO paper_account (id, balance, opened_at) VALUES (1, 500000, '2025-01-01')")
    conn.commit()
    return {"calendar": cal, "symbols": SYMBOLS, "sectors": dict(SECTORS), "info": info,
            "late_filed": date(2026, 3, 31) + timedelta(days=40)}
