"""
Pairs registry and spread snapshots.

quant_pair: pair_id, asset_a, asset_b, hedge_ratio (a number, or "ols" = estimated
over the lookback each time), spread kind (log | ratio), lookback, entry_z,
exit_z, stop_z, capital_allocation_pct, version, status (DRAFT / ACTIVE /
PAUSED / RETIRED). Parameter changes are a new version (pair_id@version).

analyze_pair(conn, pair_id, as_of) -> statarb.analyze on point-in-time bars,
stored in quant_spread (hedge ratio, spread, z-score, correlation, ADF t,
cointegration flag, half-life). screen(conn, symbols, as_of) ranks candidate
pairs by cointegration statistic -- research output, not a trading signal.

Trading a pair goes through the W3 "pairs" strategy kind (strategy_engine/kinds.py),
so every leg is a StrategyDecision -> PositionIntent -> W4 risk engine.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import date, datetime

from quant import statarb as SA

STATUSES = ("DRAFT", "ACTIVE", "PAUSED", "RETIRED")


def save_pair(conn, spec: dict) -> dict:
    pid = spec.get("pair_id") or f"{spec['asset_a']}_{spec['asset_b']}".lower()
    if not re.match(r"^[a-z0-9_]{3,64}$", pid):
        raise ValueError("pair_id: lowercase letters, digits, underscore")
    a, b = spec["asset_a"].upper(), spec["asset_b"].upper()
    if a == b:
        raise ValueError("asset_a and asset_b must differ")
    hr = spec.get("hedge_ratio", "ols")
    if hr != "ols" and not isinstance(hr, (int, float)):
        raise ValueError('hedge_ratio must be a number or "ols"')
    ez, xz, sz = float(spec.get("entry_z", 2.0)), float(spec.get("exit_z", 0.5)), float(spec.get("stop_z", 4.0))
    if not 0 <= xz < ez < sz:
        raise ValueError("need 0 <= exit_z < entry_z < stop_z")
    ver = str(spec.get("version", "1"))
    if conn.execute("SELECT 1 FROM quant_pair WHERE pair_id=? AND version=?", (pid, ver)).fetchone():
        raise ValueError(f"pair {pid}@{ver} exists; parameters are immutable per version")
    conn.execute("INSERT INTO quant_pair (pair_id,version,asset_a,asset_b,hedge_ratio,spread_kind,lookback,entry_z,"
                 "exit_z,stop_z,capital_allocation_pct,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, ver, a, b, json.dumps(hr), spec.get("spread_kind", "log"), int(spec.get("lookback", 120)), ez,
                  xz, sz, float(spec.get("capital_allocation_pct", 10)), "DRAFT", spec.get("notes", ""),
                  datetime.now()))
    conn.commit()
    return get_pair(conn, pid, ver)


def get_pair(conn, pid, version=None) -> dict | None:
    q = "SELECT * FROM quant_pair WHERE pair_id=?" + (" AND version=?" if version else "") + \
        " ORDER BY created_at DESC LIMIT 1"
    r = conn.execute(q, (pid, version) if version else (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["hedge_ratio"] = json.loads(d["hedge_ratio"])
    return d


def set_status(conn, pid, version, status) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute("UPDATE quant_pair SET status=? WHERE pair_id=? AND version=?", (status, pid, version))
    conn.commit()
    return get_pair(conn, pid, version)


def _bars(conn, symbols, as_of, lookback):
    from backtest.data import PriceHistory
    h = PriceHistory.load(conn, symbols, as_of, as_of, warmup_days=int(lookback * 1.6) + 10)
    return {s: h.view(as_of).history(s) for s in symbols}


def analyze_pair(conn, pid, as_of=None, version=None, store=True) -> dict:
    p = get_pair(conn, pid, version)
    if not p:
        raise ValueError(f"no pair {pid}")
    as_of = date.fromisoformat(str(as_of)[:10]) if as_of else date.today()
    bars = _bars(conn, [p["asset_a"], p["asset_b"]], as_of, p["lookback"])
    hr = None if p["hedge_ratio"] == "ols" else float(p["hedge_ratio"])
    res = SA.analyze(bars[p["asset_a"]], bars[p["asset_b"]], p["lookback"], p["spread_kind"], hr)
    res.update(pair_id=pid, version=p["version"])
    if store and res.get("ok"):
        conn.execute("INSERT INTO quant_spread (pair_id,version,as_of,hedge_ratio,spread,zscore,correlation,adf_t,"
                     "cointegrated_5pct,half_life,sessions,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(pair_id,version,as_of) DO UPDATE SET hedge_ratio=excluded.hedge_ratio,"
                     "spread=excluded.spread,zscore=excluded.zscore,correlation=excluded.correlation,"
                     "adf_t=excluded.adf_t,cointegrated_5pct=excluded.cointegrated_5pct,half_life=excluded.half_life,"
                     "sessions=excluded.sessions,created_at=excluded.created_at",
                     (pid, p["version"], res["as_of"], res["hedge_ratio"], res["spread"], res["zscore"],
                      res["correlation"], res["adf_t"], int(bool(res["cointegrated_5pct"])), res["half_life"],
                      res["sessions"], datetime.now()))
        conn.commit()
    return res


def screen(conn, symbols, as_of=None, lookback=250, min_corr=0.7, limit=20) -> list:
    """Candidate pairs among `symbols` (keep it small: n(n-1)/2 tests)."""
    symbols = sorted({s.upper() for s in symbols})[:40]
    as_of = date.fromisoformat(str(as_of)[:10]) if as_of else date.today()
    bars = _bars(conn, symbols, as_of, lookback)
    out = []
    for a, b in itertools.combinations(symbols, 2):
        r = SA.analyze(bars[a], bars[b], lookback)
        if r.get("ok") and (r["correlation"] or 0) >= min_corr and r["adf_t"] is not None:
            out.append({"asset_a": a, "asset_b": b, **{k: r[k] for k in (
                "hedge_ratio", "zscore", "correlation", "adf_t", "cointegrated_5pct", "half_life")}})
    return sorted(out, key=lambda x: x["adf_t"])[:limit]
