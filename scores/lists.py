"""
Dedicated score lists (DB-11), W28.

    crash_risk(conn, td=None, n=25)  highest CRI on the session, with each stock's CRI
        components (score_components), its CRI 5 sessions earlier, whether it is a LIVE
        holding, and the dominant driver
    top_spi(conn, td=None, n=25)     highest SPI. ai_scores.spi when the fundamentals score
        switch is on; otherwise the SPI computed on NSE fundamentals at ingestion
        (fundamental_data.spi_score, latest quarter available by the session) -- the
        source column says which
    msi_panel(conn, sessions=60)     the market-level MSI series and the latest session's
        components (stored under symbol __MARKET__ from W28)
"""

from __future__ import annotations


def _session(conn, td):
    if td:
        return str(td)[:10]
    r = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()
    return str(r[0])[:10] if r and r[0] else None


def crash_risk(conn, td=None, n=25) -> dict:
    td = _session(conn, td)
    if not td:
        return {"session": None, "rows": []}
    prev = conn.execute("SELECT DISTINCT date FROM ai_scores WHERE date<? ORDER BY date DESC LIMIT 1 OFFSET 4",
                        (td,)).fetchone()
    held = {r[0] for r in conn.execute("SELECT symbol FROM portfolio_holdings WHERE date=(SELECT MAX(date) FROM "
                                       "portfolio_holdings)")}
    rows = []
    for sym, cri, atip, sig, beta in conn.execute(
            "SELECT symbol, cri, atip_score, signal, beta_1y FROM ai_scores WHERE date=? AND cri IS NOT NULL "
            "ORDER BY cri DESC LIMIT ?", (td, int(n))):
        comps = {c: (v, w) for c, v, w in conn.execute(
            "SELECT component, value, weight FROM score_components WHERE symbol=? AND date=? AND index_name='CRI'",
            (sym, td))}
        driver = max(comps.items(), key=lambda kv: (kv[1][0] or 0) * (kv[1][1] or 0))[0] if comps else None
        p = conn.execute("SELECT cri FROM ai_scores WHERE symbol=? AND date=?", (sym, str(prev[0])[:10])).fetchone() \
            if prev else None
        rows.append({"symbol": sym, "cri": cri, "cri_5s_ago": p[0] if p else None,
                     "cri_change": round(cri - p[0], 2) if p and p[0] is not None else None, "atip": atip,
                     "signal": sig, "beta_1y": beta, "held": sym in held, "driver": driver,
                     "components": {k: v[0] for k, v in comps.items()}})
    return {"session": td, "rows": rows}


def top_spi(conn, td=None, n=25) -> dict:
    td = _session(conn, td)
    rows = [dict(zip(("symbol", "spi", "atip", "signal"), r)) for r in conn.execute(
        "SELECT symbol, spi, atip_score, signal FROM ai_scores WHERE date=? AND spi IS NOT NULL ORDER BY spi DESC "
        "LIMIT ?", (td, int(n)))] if td else []
    if rows:
        return {"session": td, "source": "ai_scores (fundamentals.score_enabled)", "rows": rows}
    rows = [dict(zip(("symbol", "spi", "fs", "quarter", "roe", "eps_growth_yoy", "debt_equity"), r)) for r in
            conn.execute(
                "SELECT f.symbol, f.spi_score, f.fundamental_score, f.quarter, f.roe, f.eps_growth_yoy, f.debt_equity "
                "FROM fundamental_data f JOIN (SELECT symbol, MAX(period_end) pe FROM fundamental_data WHERE "
                "spi_score IS NOT NULL AND (available_from IS NULL OR DATE(available_from)<=?) GROUP BY symbol) m "
                "ON m.symbol=f.symbol AND m.pe=f.period_end ORDER BY f.spi_score DESC LIMIT ?",
                (td or "9999-12-31", int(n)))]
    return {"session": td, "source": "fundamental_data.spi_score (NSE filings; not yet in ATIP scores)",
            "rows": rows, "note": None if rows else "no fundamentals ingested yet (python -m data.nse_filings)"}


def msi_panel(conn, sessions=60) -> dict:
    series = [{"date": str(d)[:10], "msi": m, "mh": h} for d, m, h in conn.execute(
        "SELECT date, MAX(msi), MAX(mh_score) FROM ai_scores GROUP BY date ORDER BY date DESC LIMIT ?",
        (int(sessions),))][::-1]
    last = series[-1]["date"] if series else None
    comps = [{"component": c, "value": v, "weight": w} for c, v, w in conn.execute(
        "SELECT component, value, weight FROM score_components WHERE symbol='__MARKET__' AND index_name='MSI' AND "
        "date=?", (last,))] if last else []
    return {"series": series, "session": last, "components": comps,
            "note": None if comps else "components are stored from the W28 scoring run on"}
