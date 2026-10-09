"""
Nifty total-return index, estimated from ATIP's own data (W40, PERF-001-05).

A price index ignores dividends, so a portfolio that collects them is flattered against it by the
index's dividend yield (about 1.2-1.5 % a year for the Nifty 50). NSE publishes total-return
indices (niftyindices.com), but ATIP has no feed for them. This module rebuilds one:

    TRI_t = TRI_{t-1} x (P_t + D_t) / P_{t-1}                    (NSE's TRI method)
    D_t   = P_{t-1} x sum_i w_i x d_i,t / c_i,t-1                  index dividend points on day t

    P      the index's own close (prices_daily, e.g. NIFTY50) -- the price part is exact
    w_i    the members' weights, estimated: the top `members` (50) stored stocks by market cap
           (shares outstanding as of that day x the close), picked on the first session of each
           month; weighted each day by their caps at the previous close. NSE weights by FREE-FLOAT
           cap and ATIP has no free-float data, so only the dividend part is an estimate.
    d_i,t  cash dividend per share with ex-date t (corporate_actions, on today's share basis, the
           same basis as prices_daily; parsed like research/scorecard.load_dividends)
    c_i,t-1 the member's close the session before

Before the corporate-action calendar's first ex-date, dividends are unknown: TRI_t then follows the
price (covered = 0 on those days) and the summary says from when dividends are included.

TABLE   index_total_return (index_symbol, date, price, tri, div_points, div_yield_bp, members,
        covered, method) -- TRI starts equal to the price on the first stored day.
BENCHMARK  "NIFTY50_TR" is read from this table by wealth/perf/data.Prices (benchmark name
        "nifty50tr" on /wealth -> Performance).

    python -m data.total_return [--index NIFTY50] [--members 50]     rebuild and print the summary
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("atip.total_return")

METHOD = "price index + cap-weighted dividends of the top members by market cap (estimate)"
SUFFIX = "_TR"
DEFAULTS = {"index": "NIFTY50", "members": 50}

DDL = (
    """CREATE TABLE IF NOT EXISTS index_total_return (
        index_symbol TEXT NOT NULL, date DATE NOT NULL, price REAL NOT NULL, tri REAL NOT NULL,
        div_points REAL NOT NULL DEFAULT 0, div_yield_bp REAL NOT NULL DEFAULT 0, members INTEGER,
        covered INTEGER NOT NULL DEFAULT 0, method TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (index_symbol, date))""",
)


def _d(v) -> date:
    return v if isinstance(v, date) and not hasattr(v, "hour") else date.fromisoformat(str(v)[:10])


def config() -> dict:
    try:
        raw = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8")).get("total_return") or {}
    except Exception:
        raw = {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


def ensure_table(conn):
    for ddl in DDL:
        conn.execute(ddl)


def tr_symbol(index: str) -> str:
    return f"{index.upper()}{SUFFIX}"


def is_tr_symbol(sym: str) -> bool:
    return str(sym).upper().endswith(SUFFIX)


# ── inputs ───────────────────────────────────────────────────────────────────

def _index_prices(conn, index):
    return [(_d(r[0]), float(r[1])) for r in conn.execute(
        "SELECT date, close FROM prices_daily WHERE symbol=? AND close>0 ORDER BY date", (index,))]


def _shares(conn) -> dict:
    """symbol -> ([dates], [shares]) of every stored shares-outstanding figure, oldest first."""
    out = {}
    try:
        rows = conn.execute("SELECT symbol, COALESCE(period_end, report_date), shares_out FROM fundamental_data "
                            "WHERE shares_out>0 ORDER BY symbol, COALESCE(period_end, report_date), id").fetchall()
    except Exception as e:                      # an install without the W27 columns
        log.warning(f"  total return: no shares outstanding ({e})")
        return {}
    for sym, d, sh in rows:
        if d is None:
            continue
        ds, vs = out.setdefault(sym, ([], []))
        dd = _d(d)
        if ds and ds[-1] == dd:
            vs[-1] = float(sh)                  # the newest row for the same date wins
        else:
            ds.append(dd)
            vs.append(float(sh))
    return out


def _shares_on(shares, sym, d):
    ds, vs = shares.get(sym, ((), ()))
    i = bisect_right(ds, d) - 1
    return vs[i] if i >= 0 else None


def _closes_on(conn, d) -> dict:
    return {s: float(c) for s, c in conn.execute("SELECT symbol, close FROM prices_daily WHERE date=? AND close>0",
                                                 (str(d),))}


def pick_members(conn, shares, d, n) -> dict:
    """{symbol: weight} -- the top n stocks by market cap on session d, cap-weighted."""
    closes = _closes_on(conn, d)
    caps = {}
    for s, c in closes.items():
        sh = _shares_on(shares, s, d)
        if sh:
            caps[s] = sh * c
    top = sorted(caps.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    tot = sum(v for _, v in top)
    return {s: v / tot for s, v in top} if tot > 0 else {}


def _dividends(conn, symbols, end) -> tuple:
    """({symbol: {ex_date: rupees per share on today's share basis}}, the calendar's first ex-date).
    Same parsing as research/scorecard.load_dividends, without its two-year window: every split / bonus
    after the ex-date (up to `end`) is applied, because prices_daily is on today's basis."""
    from research.scorecard import DIV_RE
    try:
        first = conn.execute("SELECT MIN(ex_date) FROM corporate_actions").fetchone()[0]
    except Exception:
        return {}, None
    if first is None:
        return {}, None
    keep = set(symbols)
    share = {}
    for sym, ex, pf in conn.execute("SELECT symbol, ex_date, MAX(price_factor) FROM corporate_actions WHERE kind IN "
                                    "('SPLIT','BONUS','CONSOLIDATION') AND price_factor>0 AND ex_date<=? "
                                    "GROUP BY symbol, ex_date", (str(end),)):
        if sym in keep:
            share.setdefault(sym, []).append((_d(ex), float(pf)))
    out = {}
    for sym, ex, subject in conn.execute("SELECT symbol, ex_date, subject FROM corporate_actions WHERE ex_date<=? "
                                         "AND LOWER(subject) LIKE '%dividend%'", (str(end),)):
        if sym not in keep:
            continue
        amt = sum(float(m) for m in DIV_RE.findall(subject or ""))
        if amt <= 0:
            continue
        exd = _d(ex)
        for d, pf in share.get(sym, []):
            if d > exd:
                amt *= pf                       # a later 1:2 split halves the old per-share amount
        out.setdefault(sym, {})
        out[sym][exd] = out[sym].get(exd, 0.0) + amt
    return out, _d(first)


# ── build ────────────────────────────────────────────────────────────────────

def build(conn, index: str = "NIFTY50", members: int = 50, end: date | None = None) -> list:
    """The whole series, oldest first: dicts with date, price, tri, div_points, div_yield_bp, members, covered."""
    px = _index_prices(conn, index)
    if end:
        px = [p for p in px if p[0] <= end]
    if len(px) < 2:
        return []
    shares = _shares(conn)
    # members are re-picked on the first session of each month; dividends need every member ever picked
    picks, month = {}, None
    for d, _ in px:
        if (d.year, d.month) != month:
            month = (d.year, d.month)
            picks[d] = pick_members(conn, shares, d, int(members))
    universe = sorted({s for w in picks.values() for s in w})
    divs, first = _dividends(conn, universe, px[-1][0])
    member_px = {}
    for s in universe:
        member_px[s] = {_d(r[0]): float(r[1]) for r in conn.execute(
            "SELECT date, close FROM prices_daily WHERE symbol=? AND close>0 AND date>=? AND date<=?",
            (s, str(px[0][0] - timedelta(days=10)), str(px[-1][0])))}
    out = [{"date": px[0][0], "price": px[0][1], "tri": px[0][1], "div_points": 0.0, "div_yield_bp": 0.0,
            "members": len(picks.get(px[0][0], {})), "covered": int(bool(first and px[0][0] >= first))}]
    members_now = list(picks[px[0][0]])
    for (d0, p0), (d1, p1) in zip(px, px[1:]):
        if picks.get(d1):
            members_now = list(picks[d1])
        covered = bool(first and d1 >= first)
        yld = 0.0
        if covered:
            # weights from the members' caps at the previous close (NSE's weights move with prices too)
            caps = {}
            for s in members_now:
                c0, sh = member_px.get(s, {}).get(d0), _shares_on(shares, s, d0)
                if c0 and sh:
                    caps[s] = c0 * sh
            tot = sum(caps.values())
            for s, cap in caps.items():
                amt = divs.get(s, {}).get(d1)
                if amt:
                    yld += cap / tot * amt / member_px[s][d0]
        pts = p0 * yld
        prev = out[-1]["tri"]
        out.append({"date": d1, "price": p1, "tri": prev * (p1 + pts) / p0, "div_points": pts,
                    "div_yield_bp": yld * 1e4, "members": len(members_now), "covered": int(covered)})
    return out


def store(conn, index: str, rows: list) -> int:
    ensure_table(conn)
    conn.execute("DELETE FROM index_total_return WHERE index_symbol=?", (index,))
    conn.executemany(
        "INSERT INTO index_total_return (index_symbol, date, price, tri, div_points, div_yield_bp, members, covered, "
        "method) VALUES (?,?,?,?,?,?,?,?,?)",
        [(index, str(r["date"]), r["price"], round(r["tri"], 6), round(r["div_points"], 6),
          round(r["div_yield_bp"], 4), r["members"], r["covered"], METHOD) for r in rows])
    conn.commit()
    return len(rows)


def run(conn=None, index: str | None = None, members: int | None = None) -> dict:
    """Rebuild and store the series; the nightly job and the CLI call this."""
    cfg = config()
    index = (index or cfg["index"]).upper()
    members = int(members or cfg["members"])
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        rows = build(conn, index, members)
        if not rows:
            return {"status": "SKIPPED", "reason": f"fewer than two stored {index} closes"}
        n = store(conn, index, rows)
        return {"status": "COMPLETED", "index": index, "rows": n, **summary(rows)}
    finally:
        if own:
            conn.close()


# ── read ─────────────────────────────────────────────────────────────────────

def series(conn, index: str, start: date | None = None, end: date | None = None) -> list:
    try:
        rows = conn.execute("SELECT date, price, tri, div_points, div_yield_bp, members, covered FROM "
                            "index_total_return WHERE index_symbol=? AND date>=? AND date<=? ORDER BY date",
                            (index.upper(), str(start or date(1990, 1, 1)), str(end or date(2100, 1, 1)))).fetchall()
    except Exception:
        return []
    return [{"date": _d(r[0]), "price": r[1], "tri": r[2], "div_points": r[3], "div_yield_bp": r[4],
             "members": r[5], "covered": r[6]} for r in rows]


def summary(rows: list) -> dict:
    """Price vs total return over the rows, annualised, and the implied dividend yield (covered days only)."""
    if len(rows) < 2:
        return {"days": len(rows)}
    a, b = rows[0], rows[-1]
    years = max((b["date"] - a["date"]).days / 365.25, 1e-9)
    pr = b["price"] / a["price"] - 1
    tr = b["tri"] / a["tri"] - 1
    cov = [r for r in rows if r["covered"]]
    cov_years = (cov[-1]["date"] - cov[0]["date"]).days / 365.25 if len(cov) > 1 else 0
    dy = sum(r["div_yield_bp"] for r in cov) / 1e4
    return {"start": str(a["date"]), "end": str(b["date"]), "price_return_pct": round(pr * 100, 3),
            "total_return_pct": round(tr * 100, 3), "dividend_gap_pct": round((tr - pr) * 100, 3),
            "price_cagr_pct": round(((1 + pr) ** (1 / years) - 1) * 100, 3) if years >= 1 else None,
            "total_cagr_pct": round(((1 + tr) ** (1 / years) - 1) * 100, 3) if years >= 1 else None,
            "dividends_from": str(cov[0]["date"]) if cov else None,
            "implied_dividend_yield_pct": round(dy / cov_years * 100, 3) if cov_years >= 0.5 else None,
            "method": METHOD}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m data.total_return", description=__doc__.split("\n\n")[0])
    ap.add_argument("--index")
    ap.add_argument("--members", type=int)
    a = ap.parse_args(argv)
    print(json.dumps(run(index=a.index, members=a.members), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
