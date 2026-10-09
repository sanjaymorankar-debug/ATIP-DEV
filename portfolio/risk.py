"""
Portfolio risk analytics (W25): one read-only view of a book's risk.

    book_snapshot(conn, book)        PF-03  the PAPER or LIVE book: positions valued at the
                                            latest price, weights, sector exposure. The LIVE
                                            book is the latest successful broker holdings sync
                                            (portfolio/pnl.py); nothing here calls the broker.
    concentration(weights, sectors)  PF-04  max / top-5 weight, Herfindahl (HHI), effective
                                            number of positions (1/HHI), sector HHI
    correlation(R, w)                PF-09 / RK-11  pairwise correlation matrix, value-
                                            weighted average pairwise correlation, the most
                                            correlated pairs, diversification ratio
    value_at_risk(R, w, value)       RK-12  historical, parametric (normal) and Monte Carlo
                                            VaR and expected shortfall (ES / CVaR), 95% and 99%,
                                            1-day and 10-day, in % and rupees
    risk_attribution(R, w, m)        PF-07  each position's and sector's share of portfolio
                                            variance (component risk) and component VaR; beta
                                            to NIFTY50; systematic vs idiosyncratic variance
    performance_attribution(...)     PF-08  contribution by position and sector over a window
                                            (buy-and-hold of today's holdings), beta / alpha
                                            split vs NIFTY50, and a Brinson allocation /
                                            selection split against the equal-weighted ATIP
                                            universe by sector
    returns_analytics(conn, book)    PF-11  CAGR from the stored daily equity (pnl_daily),
                                            per-position XIRR from the PAPER order history
    analyse(conn, book)              everything above in one dict (the API and the snapshot)
    factor_risk(conn, book)          W40  the book through the factor risk model (quant/risk_model.py):
                                            factor + specific risk, per-factor contributions, marginal
                                            contribution per position, beta / active risk vs the Nifty 50
                                            proxy, the book's bias statistic (GET /api/portfolio/risk-model)

Returns are simple daily close-to-close returns over `lookback` sessions on the NIFTY50
calendar. A symbol with less than 80% of the window is excluded from the return-based
measures and listed in `excluded` -- the weights of the rest are NOT rescaled silently:
`coverage` says what share of the book's value the risk numbers describe.

Historical VaR is the empirical loss quantile of today's weights applied to each past
session (10-day: overlapping 10-session sums). Parametric: normal, from the sample
covariance, 10-day by sqrt(10). Monte Carlo: 20,000 multivariate-normal draws (seeded).
All VaR / ES are reported as positive losses.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime

import numpy as np

log = logging.getLogger("atip.portfolio")

BOOKS = ("PAPER", "LIVE")
BENCHMARK = "NIFTY50"
MIN_COVERAGE = 0.8
HIGH_CORRELATION = 0.7


def _sectors() -> dict:
    try:
        from execution.positions import sectors
        return sectors()
    except Exception:
        return {}


# -- the book (PF-03) ----------------------------------------------------------------

def book_snapshot(conn, book: str = "PAPER", on: date | None = None) -> dict:
    """{book, as_of, positions[{symbol, qty, mark, value, weight, sector}], positions_value,
    cash, equity, by_sector, unvalued}. Weights are of the positions' value (gross); when
    equity is known each position also carries pct_equity."""
    from portfolio.pnl import portfolio_summary
    book = str(book or "PAPER").upper()
    if book not in BOOKS:
        raise ValueError(f"book must be one of {BOOKS}")
    s = portfolio_summary(conn, book, on)
    cash = s.get("cash")
    if book == "LIVE" and cash is None:           # the last recorded LIVE cash, never a broker call
        r = conn.execute("SELECT cash, date FROM pnl_daily WHERE env='LIVE' AND cash IS NOT NULL "
                         "ORDER BY date DESC LIMIT 1").fetchone()
        if r:
            cash = float(r[0])
    pv = s["positions_value"] or 0.0
    equity = round(cash + pv, 2) if cash is not None else None
    sec = _sectors()
    pos, by_sector = [], defaultdict(float)
    for p in s["positions"]:
        if p["value"] is None:
            continue
        w = p["value"] / pv if pv else 0.0
        sector = sec.get(p["symbol"], "UNKNOWN")
        by_sector[sector] += p["value"]
        pos.append({**p, "weight": round(w, 6), "sector": sector,
                    "pct_equity": round(p["value"] / equity * 100, 3) if equity else None})
    pos.sort(key=lambda p: -p["value"])
    return {"book": book, "as_of": s["as_of"], "positions": pos, "n_positions": len(pos),
            "positions_value": round(pv, 2), "cash": cash, "equity": equity,
            "invested_pct": round(pv / equity * 100, 2) if equity else None,
            "by_sector": {k: {"value": round(v, 2), "weight": round(v / pv, 6) if pv else 0.0,
                              "pct_equity": round(v / equity * 100, 3) if equity else None}
                          for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1])},
            "unvalued": s.get("unvalued", []), "sector_map_available": bool(sec)}


# -- returns ---------------------------------------------------------------------------

def returns_matrix(conn, symbols, as_of=None, lookback: int = 250, benchmark: str = BENCHMARK) -> dict:
    """{dates, symbols, R (T x N simple returns), market (T), excluded[{symbol, reason}]}."""
    as_of = str(as_of or date.today())[:10]
    days = [str(r[0])[:10] for r in conn.execute(
        "SELECT date FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT ?",
        (benchmark, as_of, int(lookback) + 1))][::-1]
    if len(days) < 30:
        raise ValueError(f"only {len(days)} {benchmark} sessions up to {as_of}; need >= 30")
    want = list(dict.fromkeys([*symbols, benchmark]))
    closes = defaultdict(dict)
    for sym, d, c in conn.execute(
            f"SELECT symbol, date, close FROM prices_daily WHERE date>=? AND date<=? AND close>0 AND "
            f"symbol IN ({','.join('?' * len(want))})", (days[0], days[-1], *want)):
        closes[sym][str(d)[:10]] = float(c)
    keep, excluded, cols = [], [], []
    for s in symbols:
        have = closes.get(s, {})
        cov = sum(1 for d in days if d in have) / len(days)
        if cov < MIN_COVERAGE:
            excluded.append({"symbol": s, "reason": f"price history covers {cov:.0%} of the window"})
            continue
        seq, last = [], None
        for d in days:                            # carry the last close over a missing session
            last = have.get(d, last)
            seq.append(last)
        first = next(v for v in seq if v is not None)
        seq = [first if v is None else v for v in seq]
        keep.append(s)
        cols.append(seq)
    mk = [closes[benchmark].get(d) for d in days]
    for i in range(len(mk)):
        if mk[i] is None:
            mk[i] = mk[i - 1] if i else next(v for v in mk if v is not None)
    P = np.array(cols, float).T if cols else np.zeros((len(days), 0))
    R = P[1:] / P[:-1] - 1 if cols else np.zeros((len(days) - 1, 0))
    M = np.array(mk, float)
    return {"dates": days[1:], "symbols": keep, "R": R, "market": M[1:] / M[:-1] - 1, "excluded": excluded}


# -- measures (pure) ------------------------------------------------------------------

def concentration(weights: dict, sectors: dict | None = None) -> dict:
    w = np.array([v for v in weights.values() if v > 0], float)
    if not len(w):
        return {"n": 0}
    w = w / w.sum()
    hhi = float((w ** 2).sum())
    sw = defaultdict(float)
    for s, v in weights.items():
        sw[(sectors or {}).get(s, "UNKNOWN")] += v
    tot = sum(sw.values()) or 1.0
    shhi = sum((v / tot) ** 2 for v in sw.values())
    top = np.sort(w)[::-1]
    return {"n": int(len(w)), "max_weight": round(float(top[0]), 4), "top5_weight": round(float(top[:5].sum()), 4),
            "hhi": round(hhi, 4), "effective_n": round(1 / hhi, 2), "sector_hhi": round(shhi, 4),
            "effective_sectors": round(1 / shhi, 2) if shhi else None, "n_sectors": len(sw),
            "note": "effective_n = 1/HHI: the number of equal positions with the same concentration"}


def correlation(R: np.ndarray, symbols: list, w: np.ndarray) -> dict:
    n = len(symbols)
    if n < 2:
        return {"n": n, "note": "correlation needs at least 2 positions with history"}
    C = np.corrcoef(R.T)
    C = np.nan_to_num(C)
    iu = np.triu_indices(n, 1)
    ww = np.outer(w, w)[iu]
    avg_w = float((C[iu] * ww).sum() / ww.sum()) if ww.sum() > 0 else None
    sd = R.std(0, ddof=1)
    port_sd = float(np.sqrt(w @ np.cov(R.T) @ w))
    pairs = sorted(({"a": symbols[i], "b": symbols[j], "corr": round(float(C[i, j]), 3)}
                    for i, j in zip(*iu)), key=lambda p: -p["corr"])
    return {"n": n, "matrix": {"symbols": symbols, "values": np.round(C, 3).tolist()},
            "avg_pairwise": round(float(C[iu].mean()), 3), "avg_pairwise_weighted": None if avg_w is None else round(avg_w, 3),
            "max_pair": pairs[0], "high_pairs": [p for p in pairs if p["corr"] >= HIGH_CORRELATION][:20],
            "most_diversifying": pairs[-3:][::-1],
            "diversification_ratio": round(float((w * sd).sum() / port_sd), 3) if port_sd > 0 else None,
            "note": "diversification ratio = weighted average volatility / portfolio volatility (1 = none)"}


def _hist_var(pnl: np.ndarray, conf: float) -> tuple:
    losses = -pnl
    q = float(np.quantile(losses, conf))
    tail = losses[losses >= q]
    return max(q, 0.0), max(float(tail.mean()) if len(tail) else q, 0.0)


def value_at_risk(R: np.ndarray, w: np.ndarray, value: float, equity: float | None = None,
                  confidences=(0.95, 0.99), mc_draws: int = 20000, seed: int = 11) -> dict:
    """VaR / ES of a book with weights w (of `value`) as fractions and rupees."""
    if R.shape[1] == 0 or len(R) < 30:
        return {"note": "not enough history"}
    port = R @ w
    T = len(port)
    port10 = np.array([port[i:i + 10].sum() for i in range(T - 9)]) if T >= 40 else None
    mu, S = R.mean(0), np.cov(R.T) if R.shape[1] > 1 else np.array([[R.var(ddof=1)]])
    pm, ps = float(w @ mu), float(np.sqrt(max(w @ S @ w, 0)))
    rng = np.random.default_rng(seed)
    try:
        L = np.linalg.cholesky(S + np.eye(len(w)) * 1e-12)
        sims = (mu + rng.standard_normal((mc_draws, len(w))) @ L.T) @ w
    except np.linalg.LinAlgError:
        sims = rng.normal(pm, ps, mc_draws)
    out = {"sessions": T, "value": round(value, 2), "equity": equity, "daily_mean_pct": round(pm * 100, 4),
           "daily_vol_pct": round(ps * 100, 4), "annual_vol_pct": round(ps * math.sqrt(252) * 100, 2),
           "worst_day_pct": round(float(port.min()) * 100, 3), "levels": []}
    z = {0.95: 1.6449, 0.99: 2.3263, 0.975: 1.96}
    for c in confidences:
        hv, he = _hist_var(port, c)
        zc = z.get(c) or 1.6449
        pv = max(zc * ps - pm, 0.0)
        pe = max(ps * math.exp(-zc * zc / 2) / (math.sqrt(2 * math.pi) * (1 - c)) - pm, 0.0)
        mv, me = _hist_var(sims, c)
        row = {"confidence": c,
               "historical": {"var_pct": hv, "es_pct": he}, "parametric": {"var_pct": pv, "es_pct": pe},
               "monte_carlo": {"var_pct": mv, "es_pct": me}}
        if port10 is not None:
            h10v, h10e = _hist_var(port10, c)
            row["historical_10d"] = {"var_pct": h10v, "es_pct": h10e}
        row["parametric_10d"] = {"var_pct": pv * math.sqrt(10), "es_pct": pe * math.sqrt(10)}
        for k, v in list(row.items()):
            if isinstance(v, dict):
                row[k] = {"var_pct": round(v["var_pct"] * 100, 3), "es_pct": round(v["es_pct"] * 100, 3),
                          "var_value": round(v["var_pct"] * value, 2), "es_value": round(v["es_pct"] * value, 2),
                          "var_pct_equity": round(v["var_pct"] * value / equity * 100, 3) if equity else None}
        out["levels"].append(row)
    return out


def risk_attribution(R: np.ndarray, symbols: list, w: np.ndarray, market: np.ndarray, sectors: dict,
                     value: float) -> dict:
    if not symbols:
        return {"positions": []}
    S = np.cov(R.T) if len(symbols) > 1 else np.array([[R.var(ddof=1)]])
    var_p = float(w @ S @ w)
    sd_p = math.sqrt(var_p) if var_p > 0 else 0.0
    mrc = S @ w
    comp = w * mrc / var_p if var_p > 0 else np.zeros(len(w))
    mv = market.var(ddof=1)
    betas = np.array([np.cov(R[:, i], market)[0, 1] / mv if mv > 0 else 0.0 for i in range(len(symbols))])
    beta_p = float(w @ betas)
    sys_share = beta_p ** 2 * mv / var_p if var_p > 0 else None
    rows, by_sec = [], defaultdict(lambda: {"weight": 0.0, "risk_share": 0.0})
    for i, s in enumerate(symbols):
        sec = sectors.get(s, "UNKNOWN")
        by_sec[sec]["weight"] += float(w[i])
        by_sec[sec]["risk_share"] += float(comp[i])
        rows.append({"symbol": s, "sector": sec, "weight": round(float(w[i]), 4),
                     "vol_annual_pct": round(float(R[:, i].std(ddof=1)) * math.sqrt(252) * 100, 2),
                     "beta": round(float(betas[i]), 3), "risk_share": round(float(comp[i]), 4),
                     "risk_to_weight": round(float(comp[i] / w[i]), 2) if w[i] > 0 else None,
                     "component_var95_value": round(float(1.6449 * w[i] * mrc[i] / sd_p * value), 2) if sd_p else None})
    rows.sort(key=lambda r: -r["risk_share"])
    return {"portfolio_beta": round(beta_p, 3),
            "systematic_share": None if sys_share is None else round(min(sys_share, 1.0), 4),
            "idiosyncratic_share": None if sys_share is None else round(max(1 - sys_share, 0.0), 4),
            "positions": rows,
            "sectors": [{"sector": k, "weight": round(v["weight"], 4), "risk_share": round(v["risk_share"], 4)}
                        for k, v in sorted(by_sec.items(), key=lambda kv: -kv[1]["risk_share"])],
            "note": "risk_share = w_i (Sigma w)_i / w'Sigma w; shares sum to 1. risk_to_weight > 1: the "
                    "position adds more risk than its weight"}


def performance_attribution(conn, snap: dict, days: int = 60, as_of=None) -> dict:
    """Buy-and-hold attribution of today's holdings over the last `days` sessions."""
    syms = [p["symbol"] for p in snap["positions"]]
    if not syms:
        return {"note": "empty book"}
    as_of = str(as_of or snap["as_of"])[:10]
    rm = returns_matrix(conn, syms, as_of, days)
    idx = [syms.index(s) for s in rm["symbols"]]
    if not idx:
        return {"note": "no position has enough price history", "excluded": rm["excluded"]}
    w0 = np.array([snap["positions"][i]["weight"] for i in idx], float)
    w0 = w0 / w0.sum()
    growth = np.prod(1 + rm["R"], axis=0) - 1
    # today's weights are the END weights; back out the start weights of a buy-and-hold book
    ws = w0 / (1 + growth)
    ws = ws / ws.sum()
    contrib = ws * growth
    port = float(contrib.sum())
    mkt = float(np.prod(1 + rm["market"]) - 1)
    pr = rm["R"] @ ws
    mv = rm["market"].var(ddof=1)
    beta = float(np.cov(pr, rm["market"])[0, 1] / mv) if mv > 0 else 0.0
    sec = _sectors()
    rows, by_sec = [], defaultdict(lambda: {"weight": 0.0, "contribution": 0.0})
    for k, i in enumerate(idx):
        s = rm["symbols"][k]
        by_sec[sec.get(s, "UNKNOWN")]["weight"] += float(ws[k])
        by_sec[sec.get(s, "UNKNOWN")]["contribution"] += float(contrib[k])
        rows.append({"symbol": s, "start_weight": round(float(ws[k]), 4), "return_pct": round(float(growth[k]) * 100, 2),
                     "contribution_pct": round(float(contrib[k]) * 100, 3)})
    rows.sort(key=lambda r: -r["contribution_pct"])
    brinson = _brinson(conn, by_sec, rm, as_of, days, sec)
    return {"window_sessions": len(rm["dates"]), "from": rm["dates"][0], "to": rm["dates"][-1],
            "portfolio_return_pct": round(port * 100, 3), "benchmark": BENCHMARK,
            "benchmark_return_pct": round(mkt * 100, 3), "active_return_pct": round((port - mkt) * 100, 3),
            "beta": round(beta, 3), "beta_return_pct": round(beta * mkt * 100, 3),
            "alpha_return_pct": round((port - beta * mkt) * 100, 3),
            "positions": rows,
            "sectors": [{"sector": k, "weight": round(v["weight"], 4),
                         "contribution_pct": round(v["contribution"] * 100, 3)}
                        for k, v in sorted(by_sec.items(), key=lambda kv: -kv[1]["contribution"])],
            "brinson": brinson, "excluded": rm["excluded"],
            "method": "buy-and-hold of today's holdings over the window (trades inside the window are not "
                      "replayed); contributions sum to the portfolio return"}


def _brinson(conn, by_sec, rm, as_of, days, sec) -> dict | None:
    """Brinson-Fachler vs the equal-weighted tracked universe (benchmark sector weight =
    share of universe names; sector return = equal-weighted mean)."""
    try:
        from data.dhan import get_tracked_symbols
        uni = [s for s in get_tracked_symbols(conn) if s in sec]
    except Exception:
        return None
    if len(uni) < 50:
        return None
    u = returns_matrix(conn, uni, as_of, days)
    g = np.prod(1 + u["R"], axis=0) - 1
    bs = defaultdict(list)
    for s, r in zip(u["symbols"], g):
        bs[sec.get(s, "UNKNOWN")].append(float(r))
    nb = sum(len(v) for v in bs.values())
    rb_tot = float(np.mean(g))
    rows, alloc_t, sel_t = [], 0.0, 0.0
    for s, v in by_sec.items():
        wp = v["weight"]
        rp = v["contribution"] / wp if wp else 0.0
        wb = len(bs.get(s, [])) / nb
        rb = float(np.mean(bs[s])) if bs.get(s) else rb_tot
        alloc = (wp - wb) * (rb - rb_tot)
        sel = wp * (rp - rb)
        alloc_t += alloc
        sel_t += sel
        rows.append({"sector": s, "portfolio_weight": round(wp, 4), "benchmark_weight": round(wb, 4),
                     "portfolio_return_pct": round(rp * 100, 2), "benchmark_return_pct": round(rb * 100, 2),
                     "allocation_pct": round(alloc * 100, 3), "selection_pct": round(sel * 100, 3)})
    rows.sort(key=lambda r: -(r["allocation_pct"] + r["selection_pct"]))
    return {"benchmark": f"equal-weighted ATIP universe ({len(u['symbols'])} stocks)",
            "benchmark_return_pct": round(rb_tot * 100, 3), "allocation_pct": round(alloc_t * 100, 3),
            "selection_pct": round(sel_t * 100, 3), "sectors": rows,
            "note": "sectors the book does not hold add allocation effect too; only held sectors are listed"}


def returns_analytics(conn, book: str = "PAPER", as_of=None) -> dict:
    """PF-11: CAGR / annualised return from pnl_daily equity; PAPER per-position XIRR."""
    from wealth.perf.metrics import xirr
    book = book.upper()
    out = {"book": book}
    rows = conn.execute("SELECT date, equity FROM pnl_daily WHERE env=? AND equity>0 ORDER BY date",
                        (book,)).fetchall()
    if len(rows) >= 2:
        d0, e0 = date.fromisoformat(str(rows[0][0])[:10]), float(rows[0][1])
        d1, e1 = date.fromisoformat(str(rows[-1][0])[:10]), float(rows[-1][1])
        yrs = (d1 - d0).days / 365.0
        out["equity"] = {"from": str(d0), "to": str(d1), "start": e0, "end": e1,
                         "total_return_pct": round((e1 / e0 - 1) * 100, 3),
                         "cagr_pct": round(((e1 / e0) ** (1 / yrs) - 1) * 100, 3) if yrs >= 0.25 else None,
                         "note": "CAGR only after >= 3 months of history; equity includes deposits "
                                 "for LIVE (no cash-flow ledger for the broker account)"}
    else:
        out["equity"] = {"note": f"{len(rows)} stored daily equity row(s) for {book}; CAGR needs history"}
    if book == "PAPER":
        try:
            fills = conn.execute("SELECT symbol, created_at, transaction_type, filled_qty, fill_price, brokerage "
                                 "FROM paper_order WHERE filled_qty>0 ORDER BY created_at").fetchall()
        except Exception:
            fills = []
        flows = defaultdict(list)
        for sym, at, side, q, px, fee in fills:
            d = date.fromisoformat(str(at)[:10])
            amt = float(q) * float(px or 0)
            flows[sym].append((d, -(amt + (fee or 0)) if side == "BUY" else amt - (fee or 0)))
        snap = {p["symbol"]: p for p in book_snapshot(conn, "PAPER")["positions"]}
        today = date.fromisoformat(str(as_of or date.today())[:10])
        pos = []
        for sym, fl in flows.items():
            v = (snap.get(sym) or {}).get("value") or 0.0
            allf = fl + ([(today, v)] if v else [])
            r = xirr(allf)
            invested = -sum(a for _, a in fl if a < 0)
            pos.append({"symbol": sym, "invested": round(invested, 2), "current_value": round(v, 2),
                        "net_gain": round(sum(a for _, a in allf), 2),
                        "xirr_pct": None if r is None else round(r * 100, 2)})
        pos.sort(key=lambda p: -(p["net_gain"]))
        allflows = [f for fl in flows.values() for f in fl]
        tv = sum(p["value"] or 0 for p in snap.values())
        r = xirr(allflows + ([(today, tv)] if tv else []))
        out["xirr"] = {"book_xirr_pct": None if r is None else round(r * 100, 2), "positions": pos,
                       "note": "money-weighted, from paper fills + today's value"}
    else:
        out["xirr"] = {"note": "the LIVE book has no trade ledger in ATIP (holdings snapshots only); "
                               "see /wealth performance (W15.5) for ledger-based XIRR"}
    return out


# -- the whole view ---------------------------------------------------------------------

def analyse(conn, book: str = "PAPER", lookback: int = 250, attribution_days: int = 60, as_of=None,
            weights: dict | None = None, value: float | None = None) -> dict:
    """Everything for one book. `weights` (+ `value`) analyses a hypothetical book instead
    (the optimiser's output, a what-if)."""
    if weights:
        tot = sum(v for v in weights.values() if v > 0)
        sec = _sectors()
        value = float(value or 1_000_000)
        snap = {"book": "WHAT_IF", "as_of": str(as_of or date.today()), "positions_value": value, "equity": value,
                "positions": [{"symbol": s, "weight": v / tot, "value": value * v / tot, "sector": sec.get(s, "UNKNOWN")}
                              for s, v in weights.items() if v > 0], "cash": 0.0}
        snap["n_positions"] = len(snap["positions"])
        snap["invested_pct"] = 100.0
    else:
        snap = book_snapshot(conn, book, as_of)
    out = {"book": snap["book"], "as_of": snap["as_of"], "computed_at": datetime.now().isoformat(timespec="seconds"),
           "exposure": snap}
    if not snap["positions"]:
        out["note"] = f"the {snap['book']} book holds nothing"
        return out
    sec = _sectors()
    wmap = {p["symbol"]: p["weight"] for p in snap["positions"]}
    out["concentration"] = concentration(wmap, sec)
    rm = returns_matrix(conn, list(wmap), as_of or snap["as_of"], lookback)
    syms = rm["symbols"]
    w = np.array([wmap[s] for s in syms], float)
    cov_share = float(w.sum())
    out["coverage"] = {"symbols_with_history": len(syms), "of": len(wmap), "value_share": round(cov_share, 4),
                       "excluded": rm["excluded"], "sessions": len(rm["dates"])}
    if not syms:
        out["note"] = "no position has enough price history for return-based risk"
        return out
    wn = w / w.sum()
    covered_value = snap["positions_value"] * cov_share
    out["correlation"] = correlation(rm["R"], syms, wn)
    out["var"] = value_at_risk(rm["R"], wn, covered_value, snap.get("equity"))
    out["risk_attribution"] = risk_attribution(rm["R"], syms, wn, rm["market"], sec, covered_value)
    if not weights:
        try:
            out["performance_attribution"] = performance_attribution(conn, snap, attribution_days, as_of)
        except Exception as e:
            out["performance_attribution"] = {"error": str(e)}
        try:
            out["returns"] = returns_analytics(conn, snap["book"], as_of)
        except Exception as e:
            out["returns"] = {"error": str(e)}
    return out


def factor_risk(conn, book: str = "PAPER") -> dict:
    """W40: the factor risk model's view of the PAPER or LIVE book (quant/risk_model.py): total risk =
    factor + specific, each factor's contribution x_k (F X'w)_k, each position's marginal contribution
    to risk, beta and active risk against the Nifty 50 proxy (or the index), and the bias statistic of
    today's weights over the stored forecasts. Weights are of the positions' value, as book_snapshot;
    positions the model does not cover are listed in coverage, not dropped silently."""
    from quant import risk_model as RMOD
    book = str(book or "PAPER").upper()
    if book not in BOOKS:
        raise ValueError(f"book must be one of {BOOKS}")
    snap = book_snapshot(conn, book)
    base = {"portfolio": book, "weights_as_of": snap["as_of"], "positions_value": snap["positions_value"],
            "equity": snap["equity"], "unvalued": snap.get("unvalued", [])}
    if not snap["positions"]:
        return {"status": "EMPTY", **base, "note": f"the {book} book holds nothing"}
    weights = {p["symbol"]: p["weight"] for p in snap["positions"]}
    cfg = RMOD.settings()
    out = {**RMOD.decompose_weights(conn, weights, cfg=cfg, label=book), **base}
    if out.get("status") != "OK":
        return out
    covered_value = snap["positions_value"] * (out["coverage"]["value_share"] or 0.0)
    r = out["risk"]
    out["risk_value"] = {"covered_value": round(covered_value, 2),
                         "vol_1d_value": round((r["total_vol_pct_daily"] or 0) / 100 * covered_value, 2),
                         "var95_1d_value": round((r["var95_1d_pct"] or 0) / 100 * covered_value, 2),
                         "note": "normal 1-day 95% VaR from the factor model forecast (RK-12's historical / "
                                 "Monte Carlo VaR is beside it)"}
    try:
        out["bias"] = RMOD.bias_test(conn, cfg, weights=weights, label=book)
    except Exception as e:                           # the decomposition stands without its back-test
        out["bias"] = {"error": str(e)}
    out["bias_test"] = (RMOD.get_state(conn, "bias") or {}).get("summary")
    return out


def headline(a: dict) -> dict:
    """The few numbers a snapshot / alert / dashboard needs."""
    lv = {l["confidence"]: l for l in (a.get("var") or {}).get("levels", [])}
    l95 = lv.get(0.95, {}).get("historical") or {}
    l99 = lv.get(0.99, {}).get("historical") or {}
    c = a.get("concentration") or {}
    co = a.get("correlation") or {}
    ra = a.get("risk_attribution") or {}
    ex = a.get("exposure") or {}
    return {"n_positions": ex.get("n_positions"), "positions_value": ex.get("positions_value"),
            "equity": ex.get("equity"), "invested_pct": ex.get("invested_pct"),
            "var95_pct": l95.get("var_pct"), "var95_value": l95.get("var_value"), "es95_pct": l95.get("es_pct"),
            "var99_pct": l99.get("var_pct"), "es99_value": l99.get("es_value"),
            "var95_pct_equity": l95.get("var_pct_equity"),
            "annual_vol_pct": (a.get("var") or {}).get("annual_vol_pct"),
            "max_weight": c.get("max_weight"), "effective_n": c.get("effective_n"), "hhi": c.get("hhi"),
            "avg_correlation": co.get("avg_pairwise_weighted"), "beta": ra.get("portfolio_beta"),
            "coverage": (a.get("coverage") or {}).get("value_share")}


def store_snapshot(conn, a: dict) -> dict:
    h = headline(a)
    conn.execute("INSERT OR REPLACE INTO portfolio_risk_snapshot (as_of, book, headline_json, analysis_json, "
                 "created_at) VALUES (?,?,?,?,?)",
                 (a["as_of"], a["book"], json.dumps(h), json.dumps(a, default=str), datetime.now()))
    conn.commit()
    return h


def run_scheduled(trade_date=None) -> dict:
    """Post-market: a risk snapshot of each non-empty book, plus an alert when a book's
    95% 1-day historical VaR (% of equity) passes portfolio_risk.var95_alert_pct.
    SKIPPED unless config portfolio_risk.snapshot_enabled."""
    from db.schema import get_connection
    cfg = settings()
    if not cfg["snapshot_enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "portfolio_risk.snapshot_enabled is false"}
    conn = get_connection()
    done = {}
    try:
        for b in BOOKS:
            try:
                a = analyse(conn, b)
            except Exception as e:
                done[b] = f"error: {e}"
                continue
            if not (a.get("exposure") or {}).get("positions"):
                done[b] = "empty"
                continue
            h = store_snapshot(conn, a)
            done[b] = h
            lim = cfg.get("var95_alert_pct")
            v = h.get("var95_pct_equity") or h.get("var95_pct")
            if lim and v is not None and v > lim:
                from orders.risk import risk_alert
                risk_alert(f"{b} book VaR above limit",
                           f"1-day 95% historical VaR {v:.2f}% of equity (alert at {lim}%); "
                           f"expected shortfall {h.get('es95_pct')}%", key=f"pfvar-{b}", severity="warning")
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": sum(1 for v in done.values() if isinstance(v, dict)), "books": done}


DEFAULTS = {"snapshot_enabled": False, "var95_alert_pct": None, "lookback_sessions": 250}


def settings() -> dict:
    try:
        from orders.environment import CONFIG_PATH
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("portfolio_risk") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["snapshot_enabled"] = out["snapshot_enabled"] is True
    return out
