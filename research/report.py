"""
W39 (RS-06..RS-10) — per-stock equity research reports, the institutional workflow on
ATIP's own data:

    gather      latest price and 7-year price history, fundamentals (NSE XBRL), the stock's
                own multiple history, NSE-industry peers, beta, ATIP scores and signal,
                shareholding trend, insider trades, upcoming events
    value       research/valuation.py: DCF with bull / base / bear scenarios and a
                sensitivity grid, justified P/B for financials, peer and own-history
                multiples -> fair value, 12-month target, upside, uncertainty, rating
    write       thesis, risks and catalysts from explicit rules (every bullet names the
                number behind it), a peer table, ownership trend, price statistics, and
                SEBI-style disclosures (rating definitions, horizon, method, conflicts,
                AI use, not a registered research analyst)
    track       one research_report row per symbol per day; a row whose rating changed, or
                whose target moved more than 5%, is a "call". evaluate_targets() marks each
                call HIT / MISSED / OPEN against later prices (corporate actions applied)
                and hit_rate() summarises calls by rating (TipRanks-style success rate)

Table: research_report. Config, atip_data/config.json "research":
    {"reports_enabled": true, "valuation": {<research.valuation.DEFAULTS overrides>}}
CLI:
    python -m research report RELIANCE          print one report (JSON)
    python -m research run [--symbols ...]      write today's reports for the tracked universe
    python -m research evaluate                 mark calls hit / missed
    python -m research hit-rate
Scheduled daily at 20:40 by pipeline/scheduler.py (_schedule_w39_jobs).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta

from research import valuation as V

log = logging.getLogger(__name__)

HORIZON_DAYS = 365
CALL_TARGET_MOVE = 0.05

DDL = (
    """CREATE TABLE IF NOT EXISTS research_report (
        report_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, as_of DATE NOT NULL, price REAL, fair_value REAL,
        target_price REAL, upside_pct REAL, rating TEXT, uncertainty TEXT, moat_proxy TEXT, quality_score REAL,
        atip_signal TEXT, is_call INTEGER NOT NULL DEFAULT 0, model_version TEXT, report_json TEXT,
        outcome_status TEXT, outcome_date DATE, outcome_return_pct REAL, evaluated_at TIMESTAMP,
        created_at TIMESTAMP, UNIQUE (symbol, as_of))""",
    "CREATE INDEX IF NOT EXISTS idx_research_report_asof ON research_report(as_of)",
    "CREATE INDEX IF NOT EXISTS idx_research_report_call ON research_report(is_call, outcome_status)",
)


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("research") or {}
    except Exception:
        raw = {}
    val = raw.get("valuation") if isinstance(raw.get("valuation"), dict) else {}
    return {"reports_enabled": raw.get("reports_enabled") is not False,
            "valuation": {k: v for k, v in val.items() if k in V.DEFAULTS}}


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _num(v):
    return V._num(v)


def _d(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


# ── data gathering ───────────────────────────────────────────────────────────

class Universe:
    """
    Latest fundamentals and prices, read once for a batch of reports. `only` limits the read to
    some symbols (one report: the stock and its industry peers) -- prices_daily also holds the
    full-market bhavcopy, so the unrestricted latest-price query scans every symbol.
    """

    def __init__(self, conn, as_of=None, industry_map=None, only=None):
        self.as_of = _d(as_of) or date.today()
        if industry_map is None:
            try:
                from data.index_constituents import get_symbol_industry_map
                industry_map = get_symbol_industry_map()
            except Exception as e:
                log.warning(f"  industry map unavailable: {e}")
                industry_map = {}
        self.industry = {k.upper(): v for k, v in (industry_map or {}).items()}
        only = sorted({s.upper() for s in only}) if only else []
        in_list = f"symbol IN ({','.join('?' * len(only))})" if only else "1=1"
        self.prices = {r["symbol"]: float(r["close"]) for r in _rows(conn, f"""
            SELECT p.symbol, p.close FROM prices_daily p
            JOIN (SELECT symbol, MAX(date) d FROM prices_daily WHERE date<=? AND close>0 AND {in_list}
                  GROUP BY symbol) m
              ON p.symbol=m.symbol AND p.date=m.d""", [str(self.as_of)] + only) if r["close"]}
        self.fund = {}
        for r in _rows(conn, f"SELECT * FROM fundamental_data WHERE {in_list} "
                             "ORDER BY symbol, COALESCE(period_end, report_date), id", only):
            pe = _d(r.get("period_end") or r.get("report_date"))
            if pe and pe > self.as_of:
                continue
            self.fund[r["symbol"]] = V.normalise_fundamentals(r)     # last one wins: the newest

    def peers(self, symbol, limit=15) -> list:
        ind = self.industry.get(symbol.upper())
        if not ind:
            return []
        out = []
        for s, i in self.industry.items():
            if i != ind or s == symbol.upper() or s not in self.fund or s not in self.prices:
                continue
            val, px = self.fund[s], self.prices[s]            # already normalised
            from scores.fundamental import valuation as _mult
            m = _mult(val, px)
            shares = _num(val.get("shares_out"))
            out.append({"symbol": s, "price": px, "pe": m["pe"], "pb": m["pb"],
                        "roe_pct": round(val["roe"] * 100, 2) if val.get("roe") is not None else None,
                        "revenue_growth_pct": _num(val.get("revenue_growth_yoy")),
                        "eps_growth_pct": _num(val.get("eps_growth_yoy")),
                        "mcap_cr": round(px * shares / 1e7, 0) if shares else None})
        out.sort(key=lambda p: -(p["mcap_cr"] or 0))
        return out[:limit]


def _price_stats(conn, symbol, as_of) -> dict:
    rows = conn.execute("SELECT date, close, high, low FROM prices_daily WHERE symbol=? AND date<=? AND close>0 "
                        "ORDER BY date", (symbol, str(as_of))).fetchall()
    if not rows:
        return {}
    closes = [float(r[1]) for r in rows]
    dates = [_d(r[0]) for r in rows]
    last = closes[-1]
    out = {"last_close": last, "last_date": str(dates[-1]), "history_from": str(dates[0]),
           "history_years": round((dates[-1] - dates[0]).days / 365.25, 1)}

    def back(days):
        cut = dates[-1] - timedelta(days=days)
        for d, c in zip(reversed(dates), reversed(closes)):
            if d <= cut:
                return c
        return None
    one = back(365)
    yr = [c for d, c in zip(dates, closes) if d > dates[-1] - timedelta(days=365)]
    out["high_52w"], out["low_52w"] = (max(yr), min(yr)) if yr else (None, None)
    out["return_1y_pct"] = round((last / one - 1) * 100, 2) if one else None
    for n in (3, 5, 7):
        p = back(int(365.25 * n))
        out[f"cagr_{n}y_pct"] = round(((last / p) ** (1 / n) - 1) * 100, 2) if p else None
    if len(yr) > 20:
        rets = [math.log(b / a) for a, b in zip(yr, yr[1:]) if a > 0 and b > 0]
        mu = sum(rets) / len(rets)
        out["volatility_1y_pct"] = round(math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1) * 252) * 100, 1)
        peak, mdd = yr[0], 0.0
        for c in yr:
            peak = max(peak, c)
            mdd = min(mdd, c / peak - 1)
        out["max_drawdown_1y_pct"] = round(mdd * 100, 1)
    if len(closes) >= 200:
        out["above_200dma"] = last > sum(closes[-200:]) / 200
    return out


def _own_multiples(conn, symbol, history):
    """P/E and P/B as they were at each quarter end (the stored pe_ratio uses one price for every quarter)."""
    for h in history:
        pe_d = _d(h.get("period_end") or h.get("report_date"))
        h["pe"] = h["pb"] = None
        if not pe_d:
            continue
        r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? AND date>? AND close>0 "
                         "ORDER BY date DESC LIMIT 1", (symbol, str(pe_d), str(pe_d - timedelta(days=10)))).fetchone()
        px = float(r[0]) if r else None
        if px and _num(h.get("eps_ttm")) and h["eps_ttm"] > 0:
            h["pe"] = round(px / h["eps_ttm"], 2)
        if px and _num(h.get("book_value_ps")) and h["book_value_ps"] > 0:
            h["pb"] = round(px / h["book_value_ps"], 2)


def _latest(conn, sql, args):
    r = _rows(conn, sql, args)
    return r[0] if r else {}


def for_symbol(conn, symbol: str, as_of=None, industry_map=None) -> Universe:
    """A Universe holding just `symbol` and its NSE-industry peers."""
    if industry_map is None:
        try:
            from data.index_constituents import get_symbol_industry_map
            industry_map = get_symbol_industry_map()
        except Exception as e:
            log.warning(f"  industry map unavailable: {e}")
            industry_map = {}
    sym = symbol.upper()
    ind = {k.upper(): v for k, v in industry_map.items()}
    peers = {s for s, i in ind.items() if ind.get(sym) and i == ind[sym]}
    return Universe(conn, as_of, ind, only=peers | {sym})


def gather(conn, symbol: str, uni: Universe | None = None) -> dict:
    symbol = symbol.upper()
    uni = uni or for_symbol(conn, symbol)
    as_of = uni.as_of
    fund = uni.fund.get(symbol) or {}
    history = _rows(conn, "SELECT quarter, period_end, report_date, roe, roce, debt_equity, revenue_cr, profit_cr, "
                          "eps_ttm, book_value_ps, source FROM fundamental_data WHERE symbol=? "
                          "ORDER BY COALESCE(period_end, report_date) DESC, id DESC LIMIT 28", (symbol,))
    history = [V.normalise_fundamentals(h) for h in history
               if not _d(h.get("period_end") or h.get("report_date")) or
               _d(h.get("period_end") or h.get("report_date")) <= as_of]
    _own_multiples(conn, symbol, history)
    scores = _latest(conn, "SELECT date, atip_score, `signal`, confidence, beta_1y, fund_score, tech_score, inst_score, "
                           "news_score, regime, top_factor_1, top_factor_2, top_factor_3 FROM ai_scores "
                           "WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1", (symbol, str(as_of)))
    try:
        shp = _rows(conn, "SELECT as_of, promoter_pct, fpi_pct, mf_pct, dii_pct, retail_pct, pledged_pct "
                          "FROM shareholding_pattern WHERE symbol=? AND as_of<=? ORDER BY as_of DESC LIMIT 4",
                    (symbol, str(as_of)))
    except Exception:
        shp = []
    since = str(as_of - timedelta(days=90))
    try:
        insider = _rows(conn, "SELECT txn_type, SUM(COALESCE(value_rs,0)) AS value_rs, COUNT(*) AS n FROM insider_trade "
                              "WHERE symbol=? AND DATE(disclosed_at)>? AND DATE(disclosed_at)<=? "
                              "AND txn_type IN ('BUY','SELL') GROUP BY txn_type", (symbol, since, str(as_of)))
    except Exception:
        insider = []
    events = []
    for sql, kind in (
            ("SELECT event_date AS d, event_type AS what FROM market_event WHERE symbol=? AND event_date>=? "
             "ORDER BY event_date LIMIT 5", "event"),
            ("SELECT ex_date AS d, subject AS what FROM corporate_actions WHERE symbol=? AND ex_date>=? "
             "ORDER BY ex_date LIMIT 5", "corporate action"),
    ):
        try:
            events += [{"date": str(r["d"])[:10], "what": r["what"], "kind": kind}
                       for r in _rows(conn, sql, (symbol, str(as_of)))]
        except Exception:
            pass
    try:
        events += [{"date": str(r["d"])[:10], "what": r["subject"], "kind": "board meeting intimation"}
                   for r in _rows(conn, "SELECT broadcast_at AS d, subject FROM corporate_announcement WHERE symbol=? "
                                        "AND event_type='BOARD_MEETING' AND DATE(broadcast_at)>? "
                                        "ORDER BY broadcast_at DESC LIMIT 3", (symbol, str(as_of - timedelta(days=30))))]
    except Exception:
        pass
    try:
        held = conn.execute("SELECT qty FROM portfolio_holdings WHERE symbol=? ORDER BY date DESC LIMIT 1",
                            (symbol,)).fetchone()
    except Exception:
        held = None
    return {"symbol": symbol, "as_of": str(as_of), "price": uni.prices.get(symbol), "industry": uni.industry.get(symbol),
            "fund": fund, "history": history, "peers": uni.peers(symbol), "scores": scores, "shareholding": shp,
            "insider": {r["txn_type"]: {"value_rs": r["value_rs"], "n": r["n"]} for r in insider},
            "events": sorted(events, key=lambda e: e["date"]), "price_stats": _price_stats(conn, symbol, as_of),
            "operator_holds": bool(held and (held[0] or 0) > 0)}


# ── narrative from explicit rules ────────────────────────────────────────────

def _pct(v, nd=1):
    return f"{v:+.{nd}f}%" if v is not None else "n/a"


def thesis_risks_catalysts(g: dict, val: dict, qual: dict) -> tuple:
    f, ps, sc = g["fund"], g["price_stats"], g["scores"]
    thesis, risks = [], []
    fair, price = val.get("fair_value"), val.get("price")
    if fair and price:
        gap = (fair / price - 1) * 100
        if gap >= 10:
            thesis.append(f"Trades {gap:.0f}% below a blended fair value of ₹{fair:,.0f} "
                          f"({', '.join(val['methods'])}).")
        elif gap <= -10:
            risks.append(f"Trades {-gap:.0f}% above a blended fair value of ₹{fair:,.0f}: the price already "
                         f"assumes more than the model does.")
    peer = val["methods"].get("peer")
    if peer:
        own = (price / f["eps_ttm"]) if (peer["multiple"] == "pe" and f.get("eps_ttm") and f["eps_ttm"] > 0) else \
              (price / f["book_value_ps"]) if (peer["multiple"] == "pb" and f.get("book_value_ps")) else None
        if own:
            prem = (own / peer["peer_median"] - 1) * 100
            (thesis if prem <= -15 else risks if prem >= 30 else []).append(
                f"{peer['multiple'].upper()} {own:.1f}x against an industry median of {peer['peer_median']:.1f}x "
                f"({prem:+.0f}%, {peer['peers']} peers).")
    eg, rg = _num(f.get("eps_growth_yoy")), _num(f.get("revenue_growth_yoy"))
    if eg is not None and rg is not None:
        if eg >= 15 and rg >= 10:
            thesis.append(f"Growing: EPS {_pct(eg)} and revenue {_pct(rg)} year on year.")
        if eg < 0:
            risks.append(f"EPS shrinking {_pct(eg)} year on year.")
    if qual.get("moat_proxy") in ("WIDE", "NARROW"):
        thesis.append(f"{qual['moat_proxy'].title()} moat proxy: average return on capital "
                      f"{qual['avg_return_pct']:.1f}% over {qual['quarters']} quarters.")
    de = _num(f.get("debt_equity"))
    if de is not None and not val["financial"]:
        if de < 0.3:
            thesis.append(f"Lightly geared: debt/equity {de:.2f}.")
        elif de > 1.5:
            risks.append(f"High leverage: debt/equity {de:.2f}.")
    ic = _num(f.get("interest_coverage"))
    if ic is not None and ic < 2 and not val["financial"]:
        risks.append(f"Interest cover only {ic:.1f}x.")
    shp = g["shareholding"]
    if shp:
        pl = _num(shp[0].get("pledged_pct"))
        if pl and pl >= 5:
            risks.append(f"{pl:.1f}% of promoter holding is pledged.")
        if len(shp) >= 2 and _num(shp[0].get("promoter_pct")) is not None and _num(shp[-1].get("promoter_pct")) is not None:
            ch = shp[0]["promoter_pct"] - shp[-1]["promoter_pct"]
            if ch >= 1:
                thesis.append(f"Promoters raised their stake by {ch:.1f} pts over {len(shp)} quarters.")
            elif ch <= -2:
                risks.append(f"Promoters cut their stake by {-ch:.1f} pts over {len(shp)} quarters.")
        inst = [(_num(r.get("fpi_pct")) or 0) + (_num(r.get("mf_pct")) or 0) for r in (shp[0], shp[-1])]
        if len(shp) >= 2 and inst[0] - inst[1] >= 2:
            thesis.append(f"FPI + mutual-fund ownership up {inst[0] - inst[1]:.1f} pts over {len(shp)} quarters.")
    ins = g["insider"]
    sell, buy = (ins.get("SELL") or {}).get("value_rs") or 0, (ins.get("BUY") or {}).get("value_rs") or 0
    if sell > max(buy * 2, 1e7):
        risks.append(f"Insiders sold ₹{sell / 1e7:,.1f} cr in the last 90 days (bought ₹{buy / 1e7:,.1f} cr).")
    elif buy > max(sell * 2, 1e7):
        thesis.append(f"Insiders bought ₹{buy / 1e7:,.1f} cr in the last 90 days.")
    vol = ps.get("volatility_1y_pct")
    if vol and vol >= 45:
        risks.append(f"Volatile: {vol:.0f}% annualised over the last year.")
    if ps.get("above_200dma") is False:
        risks.append("Price is below its 200-day average.")
    if sc.get("signal"):
        tops = ", ".join(x for x in (sc.get("top_factor_1"), sc.get("top_factor_2"), sc.get("top_factor_3")) if x)
        line = f"ATIP quant signal {sc['signal']} (score {sc.get('atip_score')}, {sc.get('date')})" + \
               (f"; leading factors: {tops}." if tops else ".")
        if sc["signal"] in ("BUY", "SELL"):
            (thesis if sc["signal"] == "BUY" else risks).append(line)
    if val.get("uncertainty") in ("HIGH", "VERY_HIGH"):
        risks.append(f"Valuation uncertainty {val['uncertainty'].replace('_', ' ').lower()}: the methods "
                     f"or scenarios disagree, or inputs are missing (completeness {val['data_completeness']:.0%}).")
    catalysts = [f"{e['date']}: {e['what']} ({e['kind']})" for e in g["events"]]
    return thesis, risks, catalysts


def disclosures(g: dict, val: dict) -> list:
    return [
        f"Generated automatically by ATIP's quantitative model ({val['model_version']}) from exchange filings and "
        "market data stored by ATIP. No human analyst reviewed it, and no generative AI wrote any figure or "
        "sentence: every statement comes from a stated rule.",
        "ATIP and its operator are not registered with SEBI as a Research Analyst or Investment Adviser. This "
        "report is for the operator's own research and is not investment advice or a recommendation to anyone.",
        "Ratings: " + "; ".join(f"{k} = {v}" for k, v in V.RATING_DEFINITIONS.items()) + ". Horizon 12 months; "
        "absolute return, no benchmark.",
        "Valuation: " + ("justified P/B ((ROE - g) / (ke - g))" if val["financial"] else
                         "two-stage DCF of earnings x (1 - g/ROE), bull/base/bear weighted 25/50/25") +
        ", peer-median and own-history multiples, blended; 12-month target = fair value x (1 + ke - dividend "
        f"yield). Cost of equity {val['cost_of_equity_pct']}% (CAPM: risk-free "
        f"{val['assumptions']['risk_free_pct']}%, equity risk premium {val['assumptions']['equity_risk_premium_pct']}%).",
        "Interest: the operator's portfolio " + ("HOLDS" if g["operator_holds"] else "does not hold") +
        " this stock as of the latest holdings sync.",
        "Past hit rates are computed on ATIP's own stored prices, are not independently verified, and do not "
        "predict future results.",
    ]


def build_report(conn, symbol: str, uni: Universe | None = None, cfg: dict | None = None) -> dict:
    g = gather(conn, symbol, uni)
    cfg = cfg if cfg is not None else settings()["valuation"]
    beta = (g["scores"] or {}).get("beta_1y")
    hist_mult = [{"pe": h.get("pe"), "pb": h.get("pb")} for h in g["history"]]
    val = V.value_stock(g["fund"], g["price"], industry=g["industry"], beta=beta, peers=g["peers"],
                        history=hist_mult, cfg=cfg)
    qual = V.quality_profile(g["history"])
    thesis, risks, catalysts = thesis_risks_catalysts(g, val, qual)
    f = g["fund"]
    key = {k: f.get(k) for k in ("quarter", "period_end", "eps_ttm", "book_value_ps", "revenue_cr", "profit_cr",
                                 "revenue_growth_yoy", "eps_growth_yoy", "roe", "roce", "debt_equity",
                                 "interest_coverage", "net_margin", "dividend_yield", "source")}
    return {"symbol": g["symbol"], "as_of": g["as_of"], "industry": g["industry"], "price": g["price"],
            "rating": val["rating"], "target_price": val["target_price"], "fair_value": val["fair_value"],
            "upside_pct": val["upside_pct"], "uncertainty": val["uncertainty"], "valuation": val,
            "quality": qual, "thesis": thesis, "risks": risks, "catalysts": catalysts,
            "key_financials": key, "peers": g["peers"], "shareholding": g["shareholding"],
            "insider_90d": g["insider"], "price_stats": g["price_stats"], "atip_scores": g["scores"],
            "disclosures": disclosures(g, val), "generated_at": datetime.now().isoformat(timespec="seconds")}


# ── storage, calls and target tracking ───────────────────────────────────────

def _previous(conn, symbol, as_of):
    r = conn.execute("SELECT rating, target_price FROM research_report WHERE symbol=? AND as_of<? "
                     "ORDER BY as_of DESC LIMIT 1", (symbol, str(as_of))).fetchone()
    return (r[0], r[1]) if r else None


def save_report(conn, rep: dict) -> bool:
    """Upsert today's row. Returns whether it is a call (first report, new rating, or target moved >5%)."""
    ensure_tables(conn)
    prev = _previous(conn, rep["symbol"], rep["as_of"])
    tp = rep.get("target_price")
    is_call = rep["rating"] != "NOT_RATED" and (
        prev is None or prev[0] != rep["rating"] or
        (tp and prev[1] and abs(tp / prev[1] - 1) > CALL_TARGET_MOVE))
    conn.execute("""INSERT INTO research_report (report_id, symbol, as_of, price, fair_value, target_price, upside_pct,
                        rating, uncertainty, moat_proxy, quality_score, atip_signal, is_call, model_version,
                        report_json, outcome_status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol, as_of) DO UPDATE SET price=excluded.price, fair_value=excluded.fair_value,
                        target_price=excluded.target_price, upside_pct=excluded.upside_pct, rating=excluded.rating,
                        uncertainty=excluded.uncertainty, moat_proxy=excluded.moat_proxy,
                        quality_score=excluded.quality_score, atip_signal=excluded.atip_signal,
                        is_call=excluded.is_call, model_version=excluded.model_version,
                        report_json=excluded.report_json, outcome_status=excluded.outcome_status""",
                 (f"{rep['symbol']}:{rep['as_of']}", rep["symbol"], rep["as_of"], rep["price"], rep["fair_value"],
                  tp, rep["upside_pct"], rep["rating"], rep["uncertainty"], rep["quality"].get("moat_proxy"),
                  rep["quality"].get("quality_score"), (rep.get("atip_scores") or {}).get("signal"), int(is_call),
                  rep["valuation"]["model_version"], json.dumps(rep, default=str),
                  "OPEN" if is_call else None, datetime.now()))
    return is_call


def run_reports(symbols: list | None = None, as_of=None) -> dict:
    from db.schema import get_connection
    conn = get_connection()
    n = calls = rated = 0
    errors = []
    try:
        ensure_tables(conn)
        uni = Universe(conn, as_of)
        if not symbols:
            try:
                from data.dhan import get_tracked_symbols
                symbols = get_tracked_symbols(conn)
            except Exception:
                symbols = sorted(uni.fund)
        cfg = settings()["valuation"]
        for s in sorted(set(x.upper() for x in symbols)):
            if s not in uni.prices:
                continue
            try:
                rep = build_report(conn, s, uni, cfg)
                calls += save_report(conn, rep)
                rated += rep["rating"] != "NOT_RATED"
                n += 1
            except Exception as e:
                errors.append(f"{s}: {type(e).__name__}: {e}")
        conn.commit()
        ev = evaluate_targets(conn)
    finally:
        conn.close()
    status = "FAILED" if errors and not n else "PARTIAL" if errors else "SUCCESS"
    return {"status": status, "rows": n, "rated": rated, "calls": calls, "evaluated": ev,
            "error": "; ".join(errors[:5]) or None}


def evaluate_targets(conn, today=None) -> dict:
    """Mark open calls HIT / MISSED from the stored prices after the call (split- and bonus-adjusted)."""
    ensure_tables(conn)
    today = _d(today) or date.today()
    try:
        from data.corporate_actions import entry_factor
    except Exception:
        entry_factor = None
    done = {"HIT": 0, "MISSED": 0}
    for r in _rows(conn, "SELECT report_id, symbol, as_of, price, target_price, rating FROM research_report "
                         "WHERE is_call=1 AND outcome_status='OPEN'"):
        start, end = _d(r["as_of"]), _d(r["as_of"]) + timedelta(days=HORIZON_DAYS)
        f = entry_factor(conn, r["symbol"], r["as_of"]) if entry_factor else 1.0
        price, tp = (r["price"] or 0) * f, (r["target_price"] or 0) * f
        bars = conn.execute("SELECT date, high, low, close FROM prices_daily WHERE symbol=? AND date>? AND date<=? "
                            "ORDER BY date", (r["symbol"], str(start), str(min(end, today)))).fetchall()
        status = when = None
        up = r["rating"] in ("BUY", "ADD") or (r["rating"] == "REDUCE" and tp >= price)
        for d, hi, lo, cl in bars:
            if up and (hi or cl or 0) >= tp > 0:
                status, when = "HIT", d
                break
            if not up and 0 < (lo or cl or 0) <= tp:
                status, when = "HIT", d
                break
        if status is None and today >= end and bars:
            status, when = "MISSED", bars[-1][0]
        if status:
            last = next((b[3] for b in bars if str(b[0]) == str(when)), bars[-1][3] if bars else None)
            ret = round((last / price - 1) * 100, 2) if (last and price) else None
            conn.execute("UPDATE research_report SET outcome_status=?, outcome_date=?, outcome_return_pct=?, "
                         "evaluated_at=? WHERE report_id=?", (status, str(when)[:10], ret, datetime.now(),
                                                              r["report_id"]))
            done[status] += 1
    conn.commit()
    return done


def hit_rate(conn) -> dict:
    """Closed calls by rating: count, hits, success rate, average return to the outcome."""
    ensure_tables(conn)
    out = {}
    for r in _rows(conn, "SELECT rating, outcome_status, COUNT(*) AS n, AVG(outcome_return_pct) AS avg_ret "
                         "FROM research_report WHERE is_call=1 GROUP BY rating, outcome_status"):
        o = out.setdefault(r["rating"], {"calls": 0, "open": 0, "hit": 0, "missed": 0, "_ret": [], "_n": 0})
        o["calls"] += r["n"]
        key = {"OPEN": "open", "HIT": "hit", "MISSED": "missed"}.get(r["outcome_status"])
        if key:
            o[key] += r["n"]
        if r["outcome_status"] in ("HIT", "MISSED") and r["avg_ret"] is not None:
            o["_ret"].append(r["avg_ret"] * r["n"])
            o["_n"] += r["n"]
    for o in out.values():
        closed = o["hit"] + o["missed"]
        o["success_rate_pct"] = round(o["hit"] / closed * 100, 1) if closed else None
        o["avg_return_pct"] = round(sum(o.pop("_ret")) / o["_n"], 2) if o["_n"] else None
        o.pop("_n")
        o.pop("_ret", None)
    return out


def history(conn, symbol: str) -> list:
    """Rating and target changes (calls) for one symbol, newest first."""
    ensure_tables(conn)
    return _rows(conn, "SELECT as_of, price, rating, target_price, upside_pct, uncertainty, outcome_status, "
                       "outcome_date, outcome_return_pct FROM research_report WHERE symbol=? AND is_call=1 "
                       "ORDER BY as_of DESC", (symbol.upper(),))


def latest_ratings(conn, rating: str | None = None, limit: int = 500) -> list:
    ensure_tables(conn)
    sql = ("SELECT r.symbol, r.as_of, r.price, r.fair_value, r.target_price, r.upside_pct, r.rating, r.uncertainty, "
           "r.moat_proxy, r.quality_score, r.atip_signal FROM research_report r JOIN (SELECT symbol, MAX(as_of) m "
           "FROM research_report GROUP BY symbol) x ON r.symbol=x.symbol AND r.as_of=x.m")
    args = []
    if rating:
        sql += " WHERE r.rating=?"
        args.append(rating.upper())
    sql += " ORDER BY r.upside_pct DESC LIMIT ?"
    args.append(int(limit))
    return _rows(conn, sql, args)


def stored_report(conn, symbol: str) -> dict | None:
    ensure_tables(conn)
    r = conn.execute("SELECT report_json FROM research_report WHERE symbol=? ORDER BY as_of DESC LIMIT 1",
                     (symbol.upper(),)).fetchone()
    return json.loads(r[0]) if r and r[0] else None
