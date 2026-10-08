"""
Cross-sectional factor engine: universe -> factors -> normalization -> ranking
-> composites -> quant_factor_score.

compute(conn, as_of, factor_ids=None, universe=None, store=True)
    1. point-in-time inputs at the close of as_of: W2 PriceHistory (bars <= as_of),
       W3 FeatureContext per symbol (features are reused, not re-implemented),
       fundamentals known by as_of (factors.fundamentals_as_of), delivery %,
       NIFTY50 closes, NSE industries (cached Nifty 500 list, no network)
    2. raw factor values per symbol; cross-sectional factors (sector_mom_60)
       from the same date's cross-section
    3. each factor's own normalization spec (normalize.apply; W39: a "neutralize" list of
       factor ids, e.g. ["beta_250", "size_log", "sector"], residualizes on those factors'
       raw values of the same date -- computed here if not in the run), then
         pct    percentile of the RAW value (0..100, higher raw = higher pct)
         score  direction-adjusted percentile (higher = better for the factor:
                pct, or 100 - pct when direction = -1)  <- what strategies read
         rank   1 = best by score;  sector_rank within the NSE industry
    4. composites (composite.py) from the stored factor scores
Rows: quant_factor_score (as_of, symbol, factor_key) with raw, norm, pct, score,
rank, sector, sector_rank, universe_size. Re-running a date replaces its rows.

rankings(conn, as_of, key, top=None, bottom=None, sector=None)   top/bottom N
sector_rankings(conn, as_of, key)                                  industries ranked by mean score
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from quant import factors as FX
from quant import normalize as NZ

log = logging.getLogger("atip.quant")


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def sectors() -> dict:
    try:
        from ml.context_features import sector_map
        return sector_map()
    except Exception:
        return {}


def _universe(conn, universe):
    if isinstance(universe, (list, tuple)):
        return sorted({s.upper() for s in universe})
    from backtest.data import tracked_universe
    return list(tracked_universe(conn).symbols)


def raw_factors(conn, as_of, factor_ids, symbols) -> dict:
    """{factor_id: {symbol: raw}} at the close of as_of."""
    from backtest.data import PriceHistory
    from strategy_engine.kinds import EvalEnv
    hist = PriceHistory.load(conn, symbols, as_of, as_of, warmup_days=420)
    bench = {}
    for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=?",
                             (str(as_of - timedelta(days=420)), str(as_of))):
        bench[_d(d)] = c
    env = EvalEnv(hist, symbols, None, None, bench)
    deliv = {}
    need_deliv = "delivery_pct_20" in factor_ids
    if need_deliv:
        for s, d, p in conn.execute(
                f"SELECT symbol, date, delivery_pct FROM prices_daily WHERE date>=? AND date<=? AND symbol IN "
                f"({','.join('?' * len(symbols))}) ORDER BY date", [str(as_of - timedelta(days=45)), str(as_of)] + symbols):
            deliv.setdefault(s, []).append(p)
    fund = FX.fundamentals_as_of(conn, symbols, as_of) if any(
        "fundamentals" in FX.get(f).inputs for f in factor_ids) else {}
    deriv = FX.derivatives_as_of(conn, symbols, as_of) if any(          # W36 (AF-06)
        "derivatives" in FX.get(f).inputs for f in factor_ids) else {}
    out = {f: {} for f in factor_ids}
    for s in symbols:
        fc = env.context(s, as_of)
        if fc is None:
            continue
        fh = fund.get(s, [])
        ctx = FX.FactorContext(s, as_of, fc.bars, fc, fh[-1] if fh else None, fh, deliv.get(s), bench,
                               deriv.get(s))
        for f in factor_ids:
            fd = FX.get(f)
            if fd.cross_sectional:
                continue
            try:
                v = fd.fn(ctx)
            except Exception:
                v = None
            out[f][s] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and \
                not (isinstance(v, float) and v != v) else None
    if "sector_mom_60" in factor_ids:
        base = out.get("mom_3m") or {s: env.context(s, as_of).get("ret_60") for s in symbols
                                     if env.context(s, as_of) is not None}
        sec = sectors()
        groups = {}
        for s, v in base.items():
            if v is not None and sec.get(s):
                groups.setdefault(sec[s], []).append(v)
        out["sector_mom_60"] = {s: (sum(groups[sec[s]]) / len(groups[sec[s]]) if sec.get(s) in groups
                                    and len(groups[sec[s]]) >= 3 else None) for s in base}
    return out


def compute(conn, as_of=None, factor_ids=None, universe="tracked_current", store=True, composites=None) -> dict:
    if as_of is None:
        r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE source IN ('dhan','bhavcopy')").fetchone()[0]
        as_of = r
    as_of = _d(as_of)
    factor_ids = list(factor_ids or FX.BUILTIN_SET)
    for f in factor_ids:
        FX.get(f)
    symbols = _universe(conn, universe)
    raw = raw_factors(conn, as_of, factor_ids, symbols)
    sec = sectors()
    rows, summary = [], {}

    def exposures(spec):
        """{name: {symbol: raw}} for a spec's numeric neutralize names (factor ids), same date."""
        names = [x for x in ((spec or {}).get("neutralize") or []) if isinstance(x, str) and x != "sector"]
        missing = [x for x in names if x not in raw]
        for x in missing:
            FX.get(x)                                   # unknown name -> ValueError
        if missing:
            raw.update(raw_factors(conn, as_of, missing, symbols))
        return {x: raw[x] for x in names}
    for f in factor_ids:
        fd = FX.get(f)
        vals = raw.get(f, {})
        present = sum(1 for v in vals.values() if v is not None)
        summary[f] = {"coverage": present, "status": "DATA_PENDING" if present == 0 and fd.data_dependency else
                      ("NO_DATA" if present == 0 else "OK")}
        if present == 0:
            continue
        try:
            norm = NZ.apply(vals, fd.normalization, sec, exposures(fd.normalization))
        except ValueError as e:                         # a bad neutralize spec: reported, never silently skipped
            log.warning(f"  factor {f}: normalization failed: {e}")
            summary[f] = {**summary[f], "status": "NORMALIZATION_ERROR", "error": str(e)}
            continue
        pct = NZ.percentile(vals)
        score = {s: (p if fd.direction > 0 else 100 - p) if p is not None else None for s, p in pct.items()}
        rk = NZ.rank(score)
        srank = {}
        for grp in {sec.get(s) for s in score if sec.get(s)}:
            members = {s: score[s] for s in score if sec.get(s) == grp}
            srank.update(NZ.rank(members))
        for s, v in vals.items():
            if v is None:
                continue
            rows.append((str(as_of), s, fd.key, "factor", v, norm.get(s), pct.get(s), score.get(s), rk.get(s),
                         sec.get(s), srank.get(s), present))
    if store:
        _store(conn, as_of, rows, [FX.get(f).key for f in factor_ids])
    out = {"as_of": str(as_of), "universe": len(symbols), "factors": summary, "rows": len(rows)}
    if store and composites:
        from quant.composite import compute_composites
        out["composites"] = compute_composites(conn, as_of, composites)
    return out


def _store(conn, as_of, rows, keys):
    conn.execute(f"DELETE FROM quant_factor_score WHERE as_of=? AND factor_key IN ({','.join('?' * len(keys))})",
                 [str(as_of)] + keys)
    now = datetime.now()
    conn.executemany("INSERT INTO quant_factor_score (as_of,symbol,factor_key,kind,raw,norm,pct,score,rank,sector,"
                     "sector_rank,universe_size,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [r + (now,) for r in rows])
    conn.commit()


def rankings(conn, as_of, key, top=None, bottom=None, sector=None) -> list:
    q = "SELECT symbol, raw, score, rank, sector, sector_rank FROM quant_factor_score WHERE as_of=? AND factor_key=?"
    args = [str(as_of), key]
    if sector:
        q += " AND sector=?"; args.append(sector)
    rows = [dict(r) for r in conn.execute(q + " ORDER BY score DESC, symbol", args)]
    if top:
        return rows[:int(top)]
    if bottom:
        return rows[-int(bottom):][::-1]
    return rows


def sector_rankings(conn, as_of, key) -> list:
    rows = conn.execute("SELECT sector, AVG(score), COUNT(*) FROM quant_factor_score WHERE as_of=? AND factor_key=? "
                        "AND sector IS NOT NULL GROUP BY sector HAVING COUNT(*)>=3 ORDER BY AVG(score) DESC",
                        (str(as_of), key)).fetchall()
    return [{"sector": s, "mean_score": round(m, 2), "members": n, "rank": i + 1} for i, (s, m, n) in enumerate(rows)]


def run_scheduled(trade_date=None) -> dict:
    """quant.enabled: factor scores + composites + events + microstructure (run_job compatible)."""
    from db.schema import get_connection
    from quant.config import settings
    s = settings()
    if not s["enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "quant.enabled is false"}
    conn = get_connection()
    try:
        FX.sync(conn)
        from quant.composite import ensure_builtin
        ensure_builtin(conn)
        out = compute(conn, trade_date, None, s["universe"], True, s["composites"])
        try:
            from quant.events import sync_events
            out["events"] = sync_events(conn)
        except Exception as e:
            log.warning(f"  quant events: {e}")
        try:
            from quant.microstructure import compute_day
            out["microstructure"] = compute_day(conn, trade_date)
        except Exception as e:
            log.warning(f"  quant microstructure: {e}")
    finally:
        conn.close()
    return {"status": "SUCCESS", **out}
