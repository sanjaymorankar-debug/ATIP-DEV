"""
Formula registry (W22, SC-18) and stored score components (AF-03).

FORMULA REGISTRY. signal_log stamps every signal with model_version (git commit)
and weights_hash (scores/signal_log._weights_hash), but nothing resolved a hash back
to the weights it stood for. record(conn) stores, once per distinct hash, the whole
active weight set per index, the code version and the formula notes, in
formula_registry. get(hash) answers "which weights produced this signal".

SCORE COMPONENTS. scores/engine.weighted_score() now takes an optional label; during
run_scoring_pipeline every labelled call (VPI, MRI, RRI, CRI, ZPI, INS, ACS, ATIP, TOD)
is captured and written to score_components (symbol, date, index_name, component,
value, weight): the sub-factors that were computed and discarded before. Nothing
about how a score is computed changes.
"""

from __future__ import annotations

import json
from datetime import datetime

FORMULA_NOTES = {
    "VPI": "Volatility-Potential: V, TS (trend strength / ADX), RS, LQ, VOL, MR (mean reversion), FG, IS, NS",
    "MRI": "Momentum-Reversal: MACD (reversal: falling histogram scores high, SV-03), RSI, ADX (trend strength, "
           "read straight since SV-03), Volume, EMA, News",
    "RRI": "Recovery: Recovery (from 52-week low), RSIRecovery, Volume, Institutional, Support, News",
    "CRI": "Crash-Risk: Volatility, Debt, Distribution, WeakTrend, NegativeNews, MarketWeakness (higher = riskier; "
           "CRI is lower-is-better everywhere it ranks)",
    "ZPI": "Buy-Zone: Support, Resistance (ADX straight, SV-03), RSI, ATR (calm = high, SV-03), Volume, Trend, "
           "delivery accumulation, sector, news",
    "ATIP": "Master: VPI, SPI, RRI, MRI, MSI, ZPI, TS, FS, INS, SAFETY (100 - CRI, SV-04)",
    "TOD": "Trade of the Day: VPI, ZPI, MRI, MSI, Volume, Breakout, Sector, ACS",
}


def current_hash(conn) -> str:
    from scores.signal_log import _weights_hash
    return _weights_hash(conn)


def record(conn, code_version: str | None = None) -> dict:
    h = current_hash(conn)
    if conn.execute("SELECT 1 FROM formula_registry WHERE weights_hash=?", (h,)).fetchone():
        return {"weights_hash": h, "new": False}
    if code_version is None:
        try:
            from scores.signal_log import _git_commit
            code_version = _git_commit()
        except Exception:
            code_version = None
    w = {}
    for idx, var, wt in conn.execute("SELECT index_name, variable, weight FROM weight_config WHERE active=1 "
                                     "ORDER BY index_name, variable"):
        w.setdefault(idx, {})[var] = wt
    conn.execute("INSERT INTO formula_registry (weights_hash,weights_json,notes_json,code_version,first_seen_at) "
                 "VALUES (?,?,?,?,?)", (h, json.dumps(w, sort_keys=True), json.dumps(FORMULA_NOTES), code_version,
                                        datetime.now()))
    conn.commit()
    return {"weights_hash": h, "new": True, "indices": len(w)}


def get(conn, weights_hash: str) -> dict:
    r = conn.execute("SELECT * FROM formula_registry WHERE weights_hash=?", (weights_hash,)).fetchone()
    if not r:
        raise LookupError("unknown weights hash (recorded from W22 on; older hashes predate the registry)")
    d = dict(r)
    d["weights"] = json.loads(d.pop("weights_json"))
    d["notes"] = json.loads(d.pop("notes_json") or "{}")
    d["signals"] = conn.execute("SELECT COUNT(*) FROM signal_log WHERE weights_hash=?", (weights_hash,)).fetchone()[0]
    return d


def list_formulas(conn) -> list:
    out = []
    for r in conn.execute("SELECT weights_hash, code_version, first_seen_at FROM formula_registry ORDER BY first_seen_at"):
        d = dict(r)
        d["signals"] = conn.execute("SELECT COUNT(*) FROM signal_log WHERE weights_hash=?", (d["weights_hash"],)
                                    ).fetchone()[0]
        out.append(d)
    return out


def store_components(conn, symbol: str, trade_date, captured: dict) -> int:
    # Verification 4 Nov 2026 (VF-02): the capture is the whole record of this run for the symbol and day. An upsert
    # alone kept components a re-score no longer used (944 "Support" rows of stocks with no 200-day average), so the
    # score breakdown showed parts that did not count. Replace the day's rows instead: all of a stock's (an index such
    # as INS can drop out whole), and only the captured indices of the market-wide __MARKET__ row.
    if symbol == "__MARKET__":
        if captured:
            conn.execute(f"DELETE FROM score_components WHERE symbol=? AND date=? AND index_name IN "
                         f"({','.join('?' * len(captured))})", (symbol, str(trade_date), *captured))
    else:
        conn.execute("DELETE FROM score_components WHERE symbol=? AND date=?", (symbol, str(trade_date)))
    n = 0
    for idx, v in captured.items():
        for comp, val in v["components"].items():
            conn.execute("INSERT INTO score_components (symbol,date,index_name,component,value,weight) VALUES "
                         "(?,?,?,?,?,?) ON CONFLICT(symbol,date,index_name,component) DO UPDATE SET "
                         "value=excluded.value, weight=excluded.weight",
                         (symbol, str(trade_date), idx, comp, float(val), v["weights"].get(comp)))
            n += 1
    return n


def components(conn, symbol: str, trade_date) -> dict:
    out = {}
    for idx, comp, val, w in conn.execute("SELECT index_name, component, value, weight FROM score_components WHERE "
                                          "symbol=? AND date=? ORDER BY index_name, component",
                                          (symbol.upper(), str(trade_date))):
        out.setdefault(idx, []).append({"component": comp, "value": val, "weight": w,
                                        "contribution": round(val * w, 4) if w is not None else None})
    return out
