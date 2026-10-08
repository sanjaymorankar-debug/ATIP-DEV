"""
W39 (MP-01..MP-06) — market pulse: how global markets, FII money, derivatives positioning and
the order book bear on the Indian market today. Everything is estimated from ATIP's own stored
data, and every model reports how well it has done.

1. Global cues -> Nifty (global_cue_model)
   Rolling ridge regression (250 sessions, inputs standardised) of the Nifty's daily log return on
   the previous session's moves of S&P 500, Nasdaq, Nikkei, Hang Seng, Brent, the dollar index,
   USD/INR, gold (returns) and the US 10-year yield (change, bp) -- the moves that are known before
   India opens. Gives today's expected Nifty move, each factor's contribution, the betas, R2, and a
   walk-forward record (direction hit rate and RMSE against a zero forecast) over the last 120
   sessions. Global-Cue Score = expected move / the Nifty's daily sigma, clipped to +-3:
   STRONG_POSITIVE > 1 > POSITIVE > 0.3 > FLAT > -0.3 > NEGATIVE > -1 > STRONG_NEGATIVE.
   Research: US moves lead the Nifty's open (Granger one-way, NSE paper 39); India is a net receiver
   of spillovers (Diebold-Yilmaz); the link strengthens in crises -- hence a rolling window.

2. GIFT Nifty -> today's open (capture_gift / evaluate_gaps)
   At 08:45 and 09:05: the basis-free GIFT move = GIFT now / GIFT at 15:30 IST yesterday - 1 (GIFT
   is a future: its premium over the spot close is carry, not news). Expected gap = b x that move,
   b fitted on the stored history once 30 mornings exist (else 1). Stored in market_cue with the
   global model's view; evaluate_gaps() fills the actual open at 09:30 so the hit rate is measured.
   The macro events that hit the morning (research/event_calendar.py: FOMC, US CPI, payrolls overnight;
   an RBI decision during the session) are stored with it, and the estimate carries a band: the
   typical miss of past estimates, widened after a US release (EV-01..03).

3. FII flow pressure (fii_pressure)
   From fii_dii_market: 5-day FII net (F5) as a z-score against 250 days; the flow SURPRISE --
   the residual of FII net on the Nifty's last three returns, since flows mostly follow returns
   (research: returns -> flows in every sample) and only the unexpected part carries news; the
   selling streak; DII absorption (DII net / |FII net| on FII-selling days); the 20-day USD/INR change.
   Pressure = 0.5 z(F5) + 0.3 z(surprise) + 0.2 z(-dUSDINR20): context for days to weeks, not a
   next-day signal.

4. Derivatives positioning (positioning)
   fo_participant_oi (data/participant_oi.py): FII index-futures long % = long / (long + short), its
   5-day change and percentile, FII index-option net longs, the Client long % (contrarian);
   the Nifty futures build-up from fo_underlying_daily (price x OI: long build-up / short build-up /
   short covering / long unwinding, counted only when |dOI| > 2% and |dP| > 0.3%); the PCR z-score.
   A crowded short (long % < 15) is called bullish ONLY with a +5 pp 5-day rise AND short covering
   -- the plain "FIIs very short = bounce" rule failed all through 2026.

5. Option OI walls (oi_walls): the strikes with the most call OI above spot (resistance) and put
   OI below spot (support), nearest expiry, NIFTY and BANKNIFTY. Display only (an IIMB study found
   no max-pain effect in India).

6. Order book (data/order_pressure.py): market-wide pending buy / sell ratio and the stocks with
   persistent one-sided books -- context with a spoofing caveat.

pulse(conn) assembles all of it, plus ATIP's regime and the market gate (research/regime_gate.py),
into an overall context (RISK_ON / NEUTRAL / RISK_OFF) and the reasons. Nothing here is advice or an order.
CLI: python -m research.market_pulse [pulse|gift|evaluate|nifty-history]
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

FACTORS = {  # series in global_market_history -> (label, kind)
    "sp500": ("S&P 500", "ret"), "nasdaq": ("Nasdaq", "ret"), "nikkei": ("Nikkei", "ret"),
    "hangseng": ("Hang Seng", "ret"), "crude_brent": ("Brent crude", "ret"), "usd_index": ("Dollar index", "ret"),
    "usd_inr": ("USD/INR", "ret"), "gold": ("Gold", "ret"), "us_10y": ("US 10-year yield", "bp"),
}
WINDOW, EVAL_DAYS, RIDGE = 250, 120, 1.0

DDL = (
    """CREATE TABLE IF NOT EXISTS market_cue (
        date DATE NOT NULL, captured_at TIMESTAMP NOT NULL, gift_now REAL, gift_ref REAL, gift_ref_source TEXT,
        nifty_prev_close REAL, gift_move_pct REAL, gift_beta REAL, expected_gap_pct REAL, expected_gap_pts REAL,
        model_expected_pct REAL, cue_score REAL, cue_label TEXT, contributions_json TEXT, actual_open REAL,
        actual_gap_pct REAL, evaluated_at TIMESTAMP, PRIMARY KEY (date, captured_at))""",
)
# EV-02: the macro events that hit the morning (research/event_calendar.py) and the band given with the estimate
# GS-03: the synchronised (15:30 -> 08:45) model's estimate, research/global_sync.py
ADDED_COLUMNS = {"market_cue": {"events": "TEXT", "band_pct": "REAL", "sync_expected_pct": "REAL"}}


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)
    try:
        from db.schema import _add_missing_columns
        for table, cols in ADDED_COLUMNS.items():
            _add_missing_columns(conn, table, cols)
    except Exception as e:
        log.debug(f"market_cue column migration: {e}")


def _rows(conn, sql, args=()):
    try:
        cur = conn.execute(sql, args)
    except Exception:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _label(score, bands=((1.0, "STRONG_POSITIVE"), (0.3, "POSITIVE"), (-0.3, "FLAT"), (-1.0, "NEGATIVE")),
           low="STRONG_NEGATIVE"):
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return None
    for lo, name in bands:
        if score > lo:
            return name
    return low


# ── data ─────────────────────────────────────────────────────────────────────

def nifty_closes(conn) -> pd.Series:
    """Nifty 50 daily closes: global_market_history 'nifty50' (yfinance ^NSEI, kept by nifty_history()),
    else market_health.nifty_close."""
    rows = _rows(conn, "SELECT date, close FROM global_market_history WHERE series='nifty50' AND close>0 ORDER BY date")
    if len(rows) < 200:
        mh = _rows(conn, "SELECT date, nifty_close AS close FROM market_health WHERE nifty_close>0 ORDER BY date")
        if len(mh) > len(rows):
            rows = mh
    s = pd.Series({pd.Timestamp(str(r["date"])[:10]): float(r["close"]) for r in rows}, dtype=float)
    return s.sort_index()


def nifty_history(period="5y") -> dict:
    """Keep a long Nifty 50 daily close series (yfinance ^NSEI) in global_market_history as 'nifty50'."""
    from data.markets import _yf_download
    from db.schema import get_connection
    try:
        df = _yf_download("^NSEI", period=period, interval="1d", progress=False, auto_adjust=True)
    except Exception as e:
        return {"status": "FAILED", "rows": 0, "error": str(e)}
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    conn = get_connection()
    n = 0
    try:
        for d, v in close.dropna().items():
            conn.execute("INSERT INTO global_market_history (series, date, close, source) VALUES ('nifty50',?,?,"
                         "'yfinance') ON CONFLICT(series, date) DO UPDATE SET close=excluded.close",
                         (str(pd.Timestamp(d).date()), float(v)))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return {"status": "SUCCESS" if n else "EMPTY", "rows": n}


def _global_moves(conn) -> pd.DataFrame:
    """Daily moves per factor, indexed by the factor's own (local) trading date."""
    rows = _rows(conn, f"SELECT series, date, close FROM global_market_history WHERE series IN "
                       f"({','.join('?' * len(FACTORS))}) AND close IS NOT NULL ORDER BY date", list(FACTORS))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
    wide = df.pivot_table(index="date", columns="series", values="close", aggfunc="last").sort_index()
    out = pd.DataFrame(index=wide.index)
    for s, (_, kind) in FACTORS.items():
        if s not in wide:
            continue
        col = wide[s].dropna()
        mv = (col.diff() * 100) if kind == "bp" else np.log(col / col.shift())
        out[s] = mv
    return out


STALE_DAYS = 7                 # a factor with no new close for this long before the last Nifty session is dropped


def design(conn) -> tuple:
    """(X, y): for each Nifty session t, each factor's latest move dated strictly before t.
    A move is carried over a few rows at most (weekends, a holiday in one market); a factor whose
    data stopped updating is left out rather than repeating its last move (X.attrs['stale'])."""
    nifty = nifty_closes(conn)
    moves = _global_moves(conn)
    if len(nifty) < 30 or moves.empty:
        return pd.DataFrame(), pd.Series(dtype=float)
    y = np.log(nifty / nifty.shift()).dropna()
    X = pd.DataFrame(index=y.index)
    stale = []
    for s in moves.columns:
        col = moves[s].dropna()
        if col.empty:
            continue
        if (y.index[-1] - col.index[-1]).days > STALE_DAYS:
            stale.append(s)
            continue
        shifted = col.copy()
        shifted.index = shifted.index + pd.Timedelta(days=1)       # usable from the next calendar day on
        X[s] = shifted.reindex(X.index.union(shifted.index)).ffill(limit=4).reindex(X.index)
    keep = X.notna().all(axis=1)
    X, y = X[keep], y[keep]
    X.attrs["stale"] = stale
    return X, y


def _ridge(X: np.ndarray, y: np.ndarray, lam=RIDGE):
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    ym = y.mean()
    beta_z = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - ym))
    return beta_z, mu, sd, ym


def global_cue_model(conn, today_moves: dict | None = None) -> dict:
    X, y = design(conn)
    stale = [FACTORS[s][0] for s in X.attrs.get("stale", [])]
    if len(X) < 80:
        return {"status": "INSUFFICIENT", "observations": len(X), "stale_factors": stale,
                "reason": "needs 80+ sessions of Nifty and global closes (python -m research.market_pulse nifty-history; "
                          "data.markets.backfill_global_history)"}
    cols = list(X.columns)
    Xv, yv = X.to_numpy(float), y.to_numpy(float)
    n = len(Xv)
    w = min(WINDOW, n - 1)
    # walk-forward: fit on the w sessions before t, predict t
    preds, acts = [], []
    for t in range(max(w, n - EVAL_DAYS), n):
        b, mu, sd, ym = _ridge(Xv[t - w:t], yv[t - w:t])
        preds.append(ym + ((Xv[t] - mu) / sd) @ b)
        acts.append(yv[t])
    preds, acts = np.array(preds), np.array(acts)
    big = np.abs(acts) > 0.002
    hit = float(np.mean(np.sign(preds[big]) == np.sign(acts[big]))) if big.any() else None
    rmse, rmse0 = float(np.sqrt(np.mean((preds - acts) ** 2))), float(np.sqrt(np.mean(acts ** 2)))
    # today: fit on the latest window, apply to last night's moves
    b, mu, sd, ym = _ridge(Xv[-w:], yv[-w:])
    fitted = ym + ((Xv[-w:] - mu) / sd) @ b
    r2 = 1 - np.sum((yv[-w:] - fitted) ** 2) / np.sum((yv[-w:] - yv[-w:].mean()) ** 2)
    if today_moves is None:
        moves = _global_moves(conn)
        today_moves = {s: float(moves[s].dropna().iloc[-1]) for s in cols if s in moves and moves[s].notna().any()}
        asof = {s: str(moves[s].dropna().index[-1].date()) for s in cols if s in moves and moves[s].notna().any()}
    else:
        asof = {}
    x_now = np.array([today_moves.get(s, mu[i]) for i, s in enumerate(cols)])
    z_now = (x_now - mu) / sd
    contrib = {s: float(z_now[i] * b[i]) for i, s in enumerate(cols)}
    pred = float(ym + sum(contrib.values()))
    sigma = float(np.std(yv[-w:]))
    score = float(np.clip(pred / sigma, -3, 3)) if sigma else None
    beta_per_unit = {s: float(b[i] / sd[i]) for i, s in enumerate(cols)}
    corr60 = {}
    if n >= 60:
        for i, s in enumerate(cols):
            xs = Xv[-60:, i]
            corr60[s] = float(np.corrcoef(xs, yv[-60:])[0, 1]) if np.std(xs) > 0 and np.std(yv[-60:]) > 0 else None
    return {
        "status": "OK", "observations": n, "window": w, "r2": round(float(r2), 3),
        "expected_move_pct": round(pred * 100, 3), "nifty_sigma_pct": round(sigma * 100, 3),
        "cue_score": round(score, 2) if score is not None else None, "cue_label": _label(score),
        "contributions_pct": {FACTORS[s][0]: round(v * 100, 3) for s, v in sorted(contrib.items(),
                                                                                 key=lambda kv: -abs(kv[1]))},
        "inputs": {FACTORS[s][0]: {"move": round(today_moves.get(s, float("nan")) * (1 if FACTORS[s][1] == "bp"
                                                                                     else 100), 3),
                                   "unit": "bp" if FACTORS[s][1] == "bp" else "%", "as_of": asof.get(s)}
                   for s in cols if s in today_moves},
        "sensitivity": {FACTORS[s][0]: ({"value": round(v * 100, 4), "unit": "Nifty % per 1 bp"}
                                        if FACTORS[s][1] == "bp" else
                                        {"value": round(v, 3), "unit": "Nifty % per 1% move"})
                        for s, v in beta_per_unit.items()},
        "corr_60d": {FACTORS[s][0]: (round(v, 3) if v is not None else None) for s, v in corr60.items()},
        "stale_factors": stale,
        "walk_forward": {"sessions": len(acts), "direction_hit_rate_pct": round(hit * 100, 1) if hit is not None else None,
                         "rmse_pct": round(rmse * 100, 3), "rmse_zero_forecast_pct": round(rmse0 * 100, 3),
                         "beats_zero": rmse < rmse0},
        "note": "expected Nifty close-to-close move implied by last night's global moves; betas re-estimated daily",
    }


# ── GIFT Nifty and the open ──────────────────────────────────────────────────

def _gift_ref(conn, today: date):
    """GIFT at ~15:30 IST on the previous session (index_levels), with where it came from."""
    r = _rows(conn, "SELECT date, time, gift_nifty FROM index_levels WHERE date<? AND gift_nifty>0 "
                    "AND time<='15:35' ORDER BY date DESC, time DESC LIMIT 1", (str(today),))
    if r:
        return float(r[0]["gift_nifty"]), f"index_levels {str(r[0]['date'])[:10]} {r[0]['time']}"
    r = _rows(conn, "SELECT date, close FROM prices_daily WHERE symbol='GIFTNIFTY' AND date<? ORDER BY date DESC LIMIT 1",
              (str(today),))
    if r:
        return float(r[0]["close"]), f"GIFTNIFTY daily close {str(r[0]['date'])[:10]} (basis not removed)"
    return None, None


def gift_beta(conn) -> float:
    """Slope of the actual gap on the GIFT move over the stored mornings (1.0 until 30 exist)."""
    rows = _rows(conn, "SELECT gift_move_pct, actual_gap_pct FROM market_cue WHERE actual_gap_pct IS NOT NULL "
                       "AND gift_move_pct IS NOT NULL")
    if len(rows) < 30:
        return 1.0
    x = np.array([r["gift_move_pct"] for r in rows], float)
    y = np.array([r["actual_gap_pct"] for r in rows], float)
    if np.var(x) == 0:
        return 1.0
    return float(np.clip(np.cov(x, y, bias=True)[0, 1] / np.var(x), 0.3, 1.5))


def capture_gift(conn=None, quotes=None) -> dict:
    """Pre-open snapshot: GIFT now vs yesterday's 15:30 GIFT, plus the global model; stored in market_cue."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_tables(conn)
        today = date.today()
        if quotes is None:
            from data.dhan import fetch_index_quotes
            df = fetch_index_quotes(["gift_nifty", "nifty50"])
            quotes = {r["index"]: r for r in df.to_dict("records")} if df is not None and not df.empty else {}
        gift = (quotes.get("gift_nifty") or {}).get("ltp")
        prev_close = (quotes.get("nifty50") or {}).get("prev_close") or (quotes.get("nifty50") or {}).get("ltp")
        ref, src = _gift_ref(conn, today)
        move = (gift / ref - 1) * 100 if gift and ref else None
        beta = gift_beta(conn)
        exp = move * beta if move is not None else None
        model = global_cue_model(conn)
        try:
            from research.event_calendar import gap_band
            band = gap_band(conn, today)
        except Exception as e:
            log.warning(f"  event calendar: {e}")
            band = {"events": [], "band_pct": None}
        kinds = ",".join(e["kind"] for e in band["events"])
        try:
            from research.global_sync import model as sync_model
            sync = (sync_model(conn, today).get("today") or {}).get("expected_gap_pct")
        except Exception as e:
            log.warning(f"  synchronised global model: {e}")
            sync = None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""INSERT OR REPLACE INTO market_cue (date, captured_at, gift_now, gift_ref, gift_ref_source,
                            nifty_prev_close, gift_move_pct, gift_beta, expected_gap_pct, expected_gap_pts,
                            model_expected_pct, cue_score, cue_label, contributions_json, events, band_pct,
                            sync_expected_pct)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (str(today), now, gift, ref, src, prev_close, round(move, 3) if move is not None else None,
                      round(beta, 3), round(exp, 3) if exp is not None else None,
                      round(prev_close * exp / 100, 1) if (prev_close and exp is not None) else None,
                      model.get("expected_move_pct"), model.get("cue_score"), model.get("cue_label"),
                      json.dumps(model.get("contributions_pct") or {}), kinds, band["band_pct"], sync))
        conn.commit()
        return {"status": "SUCCESS" if gift or model.get("status") == "OK" else "EMPTY", "rows": 1,
                "gift_move_pct": move, "expected_gap_pct": exp, "model": model.get("status"),
                "events": kinds or None, "band_pct": band["band_pct"]}
    finally:
        if own:
            conn.close()


def evaluate_gaps(conn=None) -> dict:
    """Fill the actual open (the first Nifty reading between 09:15 and 09:30) into the mornings' market_cue rows."""
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    n = 0
    try:
        ensure_tables(conn)
        for r in _rows(conn, "SELECT date, captured_at, nifty_prev_close FROM market_cue WHERE actual_open IS NULL"):
            o = _rows(conn, "SELECT nifty50 FROM index_levels WHERE date=? AND time>='09:15' AND time<='09:30' "
                            "AND nifty50>0 ORDER BY time LIMIT 1", (str(r["date"])[:10],))   # a later reading is not the open
            if not o or not r["nifty_prev_close"]:
                continue
            opn = float(o[0]["nifty50"])
            conn.execute("UPDATE market_cue SET actual_open=?, actual_gap_pct=?, evaluated_at=? WHERE date=? AND "
                         "captured_at=?", (opn, round((opn / r["nifty_prev_close"] - 1) * 100, 3),
                                           datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(r["date"])[:10],
                                           str(r["captured_at"])))
            n += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"status": "SUCCESS" if n else "SKIPPED", "rows": n}


def gap_record(conn) -> dict:
    """How the stored morning estimates did against the actual open."""
    rows = _rows(conn, "SELECT expected_gap_pct, model_expected_pct, sync_expected_pct, actual_gap_pct FROM market_cue "
                       "WHERE actual_gap_pct IS NOT NULL")
    def score(key):
        pts = [(r[key], r["actual_gap_pct"]) for r in rows if r[key] is not None]
        if not pts:
            return None
        big = [(p, a) for p, a in pts if abs(a) > 0.2]
        return {"mornings": len(pts),
                "direction_hit_rate_pct": round(100 * sum(1 for p, a in big if p * a > 0) / len(big), 1) if big else None,
                "mean_abs_error_pct": round(sum(abs(p - a) for p, a in pts) / len(pts), 3)}
    out = {"gift": score("expected_gap_pct"), "global_model": score("model_expected_pct"),
           "synchronised": score("sync_expected_pct")}
    try:
        from research.event_calendar import _misses, widen_factor
        ev_m, no_m = _misses(conn)
        def mae(xs):
            return round(sum(xs) / len(xs), 3) if xs else None
        out["by_events"] = {"after_us_release": {"mornings": len(ev_m), "mean_abs_error_pct": mae(ev_m)},
                            "other": {"mornings": len(no_m), "mean_abs_error_pct": mae(no_m)},
                            "widen": widen_factor(conn)}
    except Exception as e:
        log.debug(f"gap record by events: {e}")
    return out


# ── FII flows ────────────────────────────────────────────────────────────────

def _z(series: pd.Series, x, window=250):
    s = series.dropna().iloc[-window:]
    if len(s) < 20 or s.std() == 0 or x is None:
        return None
    return float((x - s.mean()) / s.std())


def fii_pressure(conn) -> dict:
    rows = _rows(conn, "SELECT date, fii_net_cr, dii_net_cr FROM fii_dii_market WHERE fii_net_cr IS NOT NULL ORDER BY date")
    if len(rows) < 25:
        return {"status": "INSUFFICIENT", "days": len(rows), "reason": "needs 25+ days of FII/DII cash data"}
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"].astype(str).str[:10])
    df = df.set_index("date").sort_index()
    fii, dii = df["fii_net_cr"].astype(float), df["dii_net_cr"].astype(float)
    f5 = fii.rolling(5).sum()
    out = {"status": "OK", "as_of": str(df.index[-1].date()), "fii_net_cr": round(float(fii.iloc[-1]), 1),
           "dii_net_cr": round(float(dii.iloc[-1]), 1), "fii_5d_cr": round(float(f5.iloc[-1]), 1),
           "z_fii_5d": _z(f5, f5.iloc[-1])}
    streak = 0
    for v in reversed(fii.tolist()):
        if v < 0:
            streak += 1
        else:
            break
    out["selling_streak_days"] = streak
    sell = df.iloc[-20:][fii.iloc[-20:] < 0]
    out["dii_absorption_20d"] = (round(float(sell["dii_net_cr"].sum() / abs(sell["fii_net_cr"].sum())), 2)
                                 if len(sell) and sell["fii_net_cr"].sum() else None)
    m0 = df.index[-1].replace(day=1)
    y0 = df.index[-1].replace(month=1, day=1)
    out["fii_mtd_cr"] = round(float(fii[fii.index >= m0].sum()), 1)
    out["dii_mtd_cr"] = round(float(dii[dii.index >= m0].sum()), 1)
    out["fii_ytd_cr"] = round(float(fii[fii.index >= y0].sum()), 1)
    out["dii_ytd_cr"] = round(float(dii[dii.index >= y0].sum()), 1)
    # surprise: FII net minus what the Nifty's last three returns would predict
    nifty = nifty_closes(conn)
    surprise_z = None
    if len(nifty) > 30:
        r = np.log(nifty / nifty.shift())
        reg = pd.DataFrame({"f": fii, "r0": r, "r1": r.shift(1), "r2": r.shift(2)}).dropna().iloc[-WINDOW:]
        if len(reg) > 40:
            A = np.column_stack([np.ones(len(reg)), reg[["r0", "r1", "r2"]].to_numpy()])
            coef, *_ = np.linalg.lstsq(A, reg["f"].to_numpy(), rcond=None)
            resid = reg["f"].to_numpy() - A @ coef
            if resid.std():
                surprise_z = float(resid[-1] / resid.std())
                out["flow_follows_returns_r2"] = round(float(1 - resid.var() / reg["f"].var()), 3)
    out["z_surprise"] = round(surprise_z, 2) if surprise_z is not None else None
    usd = _rows(conn, "SELECT date, close FROM global_market_history WHERE series='usd_inr' ORDER BY date")
    z_fx = None
    if len(usd) > 60:
        u = pd.Series([float(x["close"]) for x in usd])
        ch20 = (u / u.shift(20) - 1).dropna()
        z_fx = _z(-ch20, -float(ch20.iloc[-1]))
        out["usd_inr"] = round(float(u.iloc[-1]), 3)
        out["usd_inr_20d_pct"] = round(float(ch20.iloc[-1]) * 100, 2)
    parts = [(0.5, out["z_fii_5d"]), (0.3, surprise_z), (0.2, z_fx)]
    tot = sum(w for w, v in parts if v is not None)
    score = sum(w * v for w, v in parts if v is not None) / tot if tot else None
    out["pressure_score"] = round(score, 2) if score is not None else None
    out["pressure_label"] = _label(score, ((1.0, "STRONG_INFLOW"), (0.3, "INFLOW"), (-0.3, "NEUTRAL"),
                                           (-1.0, "OUTFLOW")), "STRONG_OUTFLOW")
    out["z_fii_5d"] = round(out["z_fii_5d"], 2) if out["z_fii_5d"] is not None else None
    out["last_10"] = [{"date": str(d.date()), "fii_net_cr": round(float(a), 1), "dii_net_cr": round(float(b_), 1)}
                      for d, a, b_ in zip(df.index[-10:], fii.iloc[-10:], dii.iloc[-10:])][::-1]
    return out


# ── derivatives positioning ──────────────────────────────────────────────────

def buildup(price_chg_pct, oi_chg_pct, min_oi=2.0, min_p=0.3) -> str:
    if price_chg_pct is None or oi_chg_pct is None or abs(oi_chg_pct) < min_oi or abs(price_chg_pct) < min_p:
        return "NONE"
    if price_chg_pct > 0:
        return "LONG_BUILDUP" if oi_chg_pct > 0 else "SHORT_COVERING"
    return "SHORT_BUILDUP" if oi_chg_pct > 0 else "LONG_UNWINDING"


def positioning(conn) -> dict:
    out = {"status": "OK"}
    rows = _rows(conn, "SELECT date, fut_idx_long, fut_idx_short, opt_idx_call_long, opt_idx_call_short, "
                       "opt_idx_put_long, opt_idx_put_short FROM fo_participant_oi WHERE client_type='FII' ORDER BY date")
    if rows:
        df = pd.DataFrame(rows)
        L = 100 * df["fut_idx_long"] / (df["fut_idx_long"] + df["fut_idx_short"])
        cur = float(L.iloc[-1])
        out["fii"] = {
            "as_of": str(df["date"].iloc[-1])[:10], "index_futures_long_pct": round(cur, 1),
            "change_5d_pp": round(cur - float(L.iloc[-6]), 1) if len(L) > 5 else None,
            "percentile": round(float((L < cur).mean() * 100), 0) if len(L) > 20 else None,
            "history_days": len(L),
            "net_index_futures": int(df["fut_idx_long"].iloc[-1] - df["fut_idx_short"].iloc[-1]),
            "net_index_calls": int(df["opt_idx_call_long"].iloc[-1] - df["opt_idx_call_short"].iloc[-1]),
            "net_index_puts": int(df["opt_idx_put_long"].iloc[-1] - df["opt_idx_put_short"].iloc[-1]),
        }
        cl = _rows(conn, "SELECT fut_idx_long, fut_idx_short FROM fo_participant_oi WHERE client_type='Client' "
                         "ORDER BY date DESC LIMIT 1")
        if cl and (cl[0]["fut_idx_long"] or 0) + (cl[0]["fut_idx_short"] or 0):
            out["client_index_futures_long_pct"] = round(100 * cl[0]["fut_idx_long"] /
                                                          (cl[0]["fut_idx_long"] + cl[0]["fut_idx_short"]), 1)
    else:
        out["fii"] = None
    nf = _rows(conn, "SELECT date, fut_close, fut_oi, pcr_oi, max_pain, underlying_price FROM fo_underlying_daily "
                     "WHERE symbol='NIFTY' ORDER BY date DESC LIMIT 260")
    if len(nf) >= 2:
        a, b = nf[0], nf[1]
        pc = (a["fut_close"] / b["fut_close"] - 1) * 100 if a["fut_close"] and b["fut_close"] else None
        oc = (a["fut_oi"] / b["fut_oi"] - 1) * 100 if a["fut_oi"] and b["fut_oi"] else None
        out["nifty_futures"] = {"as_of": str(a["date"])[:10], "price_chg_pct": round(pc, 2) if pc is not None else None,
                                "oi_chg_pct": round(oc, 2) if oc is not None else None, "buildup": buildup(pc, oc)}
        pcrs = pd.Series([r["pcr_oi"] for r in nf if r["pcr_oi"] is not None], dtype=float)
        if len(pcrs) > 20:
            out["pcr"] = {"value": round(float(pcrs.iloc[0]), 3), "z": round(float((pcrs.iloc[0] - pcrs.mean()) /
                                                                                   pcrs.std()), 2) if pcrs.std() else None,
                          "max_pain": a.get("max_pain")}
    f = out.get("fii") or {}
    L, d5 = f.get("index_futures_long_pct"), f.get("change_5d_pp")
    bu = (out.get("nifty_futures") or {}).get("buildup")
    if L is None:
        out["read"] = None
    elif L < 15:
        out["read"] = ("CROWDED_SHORT_COVERING" if (d5 or 0) > 5 and bu == "SHORT_COVERING" else "CROWDED_SHORT")
    elif L > 65:
        out["read"] = "CROWDED_LONG"
    else:
        out["read"] = "NORMAL"
    out["read_note"] = {"CROWDED_SHORT": "FIIs heavily short index futures and not covering: a headwind, not a "
                                         "bounce signal on its own (the contrarian rule failed through 2026)",
                        "CROWDED_SHORT_COVERING": "heavily short but covering fast (long % up 5+ pp in 5 days, "
                                                  "short covering in Nifty futures): squeeze risk / bullish",
                        "CROWDED_LONG": "FIIs heavily long index futures: crowded, vulnerable to unwinding",
                        "NORMAL": "FII index-futures positioning within its usual range"}.get(out["read"])
    return out


def oi_walls(conn, symbols=("NIFTY", "BANKNIFTY")) -> dict:
    out = {}
    for sym in symbols:
        d = _rows(conn, "SELECT MAX(date) AS d FROM fo_contract_daily WHERE symbol=? AND instrument='IDO'", (sym,))
        if not d or not d[0]["d"]:
            continue
        day = str(d[0]["d"])[:10]
        exp = _rows(conn, "SELECT MIN(expiry) AS e FROM fo_contract_daily WHERE symbol=? AND date=? AND instrument='IDO'",
                    (sym, day))[0]["e"]
        opts = _rows(conn, "SELECT strike, option_type, oi, underlying FROM fo_contract_daily WHERE symbol=? AND date=? "
                           "AND instrument='IDO' AND expiry=?", (sym, day, exp))
        spot = next((o["underlying"] for o in opts if o["underlying"]), None)
        if not spot:
            continue
        calls = [o for o in opts if o["option_type"] == "CE" and o["strike"] >= spot and o["oi"]]
        puts = [o for o in opts if o["option_type"] == "PE" and o["strike"] <= spot and o["oi"]]
        rc = max(calls, key=lambda o: o["oi"]) if calls else None
        sp = max(puts, key=lambda o: o["oi"]) if puts else None
        out[sym] = {"as_of": day, "expiry": str(exp)[:10], "spot": spot,
                    "resistance": rc and {"strike": rc["strike"], "call_oi": rc["oi"],
                                          "distance_pct": round((rc["strike"] / spot - 1) * 100, 2)},
                    "support": sp and {"strike": sp["strike"], "put_oi": sp["oi"],
                                       "distance_pct": round((sp["strike"] / spot - 1) * 100, 2)}}
    return out


# ── everything together ──────────────────────────────────────────────────────

def pulse(conn) -> dict:
    ensure_tables(conn)
    gm = global_cue_model(conn)
    cue = _rows(conn, "SELECT * FROM market_cue WHERE date=? ORDER BY captured_at DESC LIMIT 1", (str(date.today()),))
    fp = fii_pressure(conn)
    pos = positioning(conn)
    walls = oi_walls(conn)
    book = {}
    try:
        from data.order_pressure import latest, persistent
        rows = latest(conn, limit=2000)
        if rows:
            b = sum(r["total_buy_qty"] or 0 for r in rows)
            s = sum(r["total_sell_qty"] or 0 for r in rows)
            pers = persistent(conn)
            book = {"as_of": max(str(r["ts"]) for r in rows), "stocks": len(rows),
                    "market_buy_sell_ratio": round(b / s, 3) if s else None,
                    "buyers_dominant": sum(1 for r in rows if (r["total_imbalance"] or 0) > 0.1),
                    "sellers_dominant": sum(1 for r in rows if (r["total_imbalance"] or 0) < -0.1),
                    "persistent_buyers": sorted(k for k, v in pers.items() if v == "BUYERS")[:20],
                    "persistent_sellers": sorted(k for k, v in pers.items() if v == "SELLERS")[:20],
                    "caveat": "pending totals include far-from-market orders and can be spoofed; imbalance in "
                              "Indian stocks predicts ~5 minutes ahead and fades within 30 (research)"}
    except Exception as e:
        log.debug(f"order book summary: {e}")
    mh = _rows(conn, "SELECT date, regime, mh_score, vix_level FROM market_health ORDER BY date DESC LIMIT 1")
    reasons, votes = [], []
    events = {}
    try:
        from research.event_calendar import gap_band, upcoming
        band = gap_band(conn, date.today())
        events = {"today": band["events"], "band": band, "upcoming": upcoming(conn, days=14)}
        for e in band["events"]:
            if e["widens_gap"]:
                reasons.append(f"{e['label']} came out overnight ({e['time_ist']} IST, {e['event_date']}): the open is "
                               f"less predictable" + (f", the gap estimate's usual miss is widened ×{band['widen']}"
                                                      if band["band_pct"] is not None else ""))
            else:
                reasons.append(f"{e['label']} at {e['time_ist']} IST today: expect a move during the session")
    except Exception as e:
        log.debug(f"event calendar: {e}")
    if gm.get("status") == "OK" and gm.get("cue_score") is not None:
        votes.append(max(-1.0, min(1.0, gm["cue_score"])))
        reasons.append(f"global cues {gm['cue_label'].replace('_', ' ').lower()} "
                       f"(expected Nifty {gm['expected_move_pct']:+.2f}%)")
    if cue and cue[0].get("expected_gap_pct") is not None:
        g = cue[0]["expected_gap_pct"]
        votes.append(max(-1.0, min(1.0, g / 0.5)))
        reasons.append(f"GIFT Nifty implies a {g:+.2f}% open")
    if fp.get("status") == "OK" and fp.get("pressure_score") is not None:
        votes.append(max(-1.0, min(1.0, fp["pressure_score"])))
        reasons.append(f"FII flows {fp['pressure_label'].replace('_', ' ').lower()} "
                       f"(5-day {fp['fii_5d_cr']:+,.0f} cr, selling streak {fp['selling_streak_days']} days)")
    if pos.get("read") in ("CROWDED_SHORT", "CROWDED_LONG", "CROWDED_SHORT_COVERING"):
        votes.append({"CROWDED_SHORT": -0.5, "CROWDED_LONG": -0.3, "CROWDED_SHORT_COVERING": 0.5}[pos["read"]])
        reasons.append(pos["read_note"])
    if mh:
        reg = (mh[0].get("regime") or "").upper()
        votes.append({"STRONG_BULL": 1.0, "BULL": 0.5, "NEUTRAL": 0.0, "BEAR": -0.5, "HIGH_RISK": -1.0}.get(reg, 0.0))
        reasons.append(f"ATIP market regime {reg or 'unknown'}")
    gate = None
    try:
        from research.regime_gate import current as gate_now
        gate = gate_now(conn)
    except Exception as e:
        log.debug(f"market gate: {e}")
    if gate and gate.get("gate") in ("OPEN", "CAUTION", "CLOSED"):
        votes.append({"OPEN": 0.5, "CAUTION": 0.0, "CLOSED": -0.5}[gate["gate"]])
        reasons.append(f"market gate {gate['gate']}: {str(gate.get('status') or '').replace('_', ' ').lower()} "
                       f"({gate.get('reason') or ''})")
    ctx = sum(votes) / len(votes) if votes else None
    return {"as_of": datetime.now().isoformat(timespec="minutes"),
            "context_score": round(ctx, 2) if ctx is not None else None,
            "context": None if ctx is None else ("RISK_ON" if ctx > 0.25 else "RISK_OFF" if ctx < -0.25 else "NEUTRAL"),
            "reasons": reasons, "global_model": gm, "gift_today": cue[0] if cue else None,
            "gap_record": gap_record(conn), "fii": fp, "positioning": pos, "oi_walls": walls, "order_book": book,
            "market_health": mh[0] if mh else None, "market_gate": gate if gate and gate.get("gate") else None,
            "events": events, "global_sync": _sync_summary(conn),
            "disclaimer": "Context from ATIP's own models and stored data; not advice."}


def _sync_summary(conn) -> dict:
    try:
        from research.global_sync import model as sync_model
        return sync_model(conn)
    except Exception as e:
        log.debug(f"synchronised global model: {e}")
        return {"status": "UNAVAILABLE", "reason": str(e)[:160]}


def main(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m research.market_pulse")
    ap.add_argument("cmd", nargs="?", default="pulse", choices=("pulse", "gift", "evaluate", "nifty-history"))
    a = ap.parse_args(argv)
    if a.cmd == "gift":
        out = capture_gift()
    elif a.cmd == "evaluate":
        out = evaluate_gaps()
    elif a.cmd == "nifty-history":
        out = nifty_history()
    else:
        from db.schema import get_connection
        conn = get_connection()
        try:
            out = pulse(conn)
        finally:
            conn.close()
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
