"""
Unsupervised learning (W24, ML-03) and anomaly detection (ML-06). numpy only.

CLUSTERS  cluster_stocks(conn, as_of, k=6, factor_keys=None, seed=7)
    k-means (k-means++ seeding, seeded) over the stocks' stored factor scores on that
    date (quant_factor_score, 0-100 percentile scores, one column per factor; a stock
    needs >= 70% of the columns, the rest take the column median). Each cluster gets a
    profile: size, the factors it is most above / below the universe on, and its
    members. Stored in ml_cluster (as_of, symbol, cluster, method) + ml_cluster_run.
    Informational: "which stocks behave alike now", not a signal.

PCA  return_pca(conn, as_of, lookback=120, n=5, symbols=None)
    principal components of the universe's daily return matrix (standardised): the
    explained variance of the first components (how much one "market factor" drives
    everything -- a concentration / regime diagnostic) and each component's top loadings.

ANOMALIES  detect_anomalies(conn, as_of, symbols=None, threshold=4.0)
    per stock, robust z-scores (median / MAD over the previous 60 sessions) of the day's
    return, volume, high-low range and gap; plus a multivariate score (the sum of the
    squared robust z's). A stock is flagged when any single z beats `threshold` or the
    multivariate score beats threshold^2. Moves on a corporate-action ex-date are
    marked explained (split / bonus). Stored in ml_anomaly. Informational.

MARKET STATES  (W39, ML-17) the clustering of MARKET days rather than stocks -- a Gaussian
    hidden Markov model of daily NIFTY50 return / realised vol / VIX -- is ml/hmm.py.
"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np


def _kmeans(Z, k, seed, iters=100, init=None):
    """Lloyd's k-means from k-means++ seeds (seeded), or from the given `init` centroids
    (W39: the market HMM's deterministic quantile seeds, ml/hmm.py)."""
    n = len(Z)
    if init is not None:
        C = np.array(init, dtype=float)
    else:
        rng = np.random.default_rng(seed)
        cent = [Z[rng.integers(n)]]
        for _ in range(1, k):
            d = np.min([((Z - c) ** 2).sum(1) for c in cent], axis=0)
            cent.append(Z[rng.choice(n, p=d / d.sum())] if d.sum() > 0 else Z[rng.integers(n)])
        C = np.array(cent)
    lab = np.zeros(n, dtype=int)
    for _ in range(iters):
        D = ((Z[:, None, :] - C[None]) ** 2).sum(2)
        new = D.argmin(1)
        if (new == lab).all() and _ > 0:
            break
        lab = new
        for j in range(k):
            if (lab == j).any():
                C[j] = Z[lab == j].mean(0)
    inertia = float(((Z - C[lab]) ** 2).sum())
    return lab, C, inertia


def cluster_stocks(conn, as_of=None, k: int = 6, factor_keys=None, seed: int = 7, store: bool = True) -> dict:
    as_of = as_of or conn.execute("SELECT MAX(as_of) FROM quant_factor_score WHERE kind='factor'").fetchone()[0]
    if not as_of:
        raise ValueError("no stored factor scores (python -m quant compute)")
    as_of = str(as_of)[:10]
    keys = factor_keys or [r[0] for r in conn.execute(
        "SELECT factor_key FROM quant_factor_score WHERE as_of=? AND kind='factor' GROUP BY factor_key "
        "HAVING COUNT(*) >= 50", (as_of,))]
    if len(keys) < 2:
        raise ValueError("fewer than 2 factors with coverage on that date")
    data = {}
    for sym, key, sc in conn.execute(f"SELECT symbol, factor_key, score FROM quant_factor_score WHERE as_of=? AND "
                                     f"factor_key IN ({','.join('?' * len(keys))})", (as_of, *keys)):
        if sc is not None:
            data.setdefault(sym, {})[key] = float(sc)
    syms = sorted(s for s, v in data.items() if len(v) >= 0.7 * len(keys))
    if len(syms) < k * 5:
        raise ValueError(f"{len(syms)} stocks with enough factor coverage: too few for {k} clusters")
    M = np.array([[data[s].get(key, np.nan) for key in keys] for s in syms])
    med = np.nanmedian(M, axis=0)
    M = np.where(np.isnan(M), med, M)
    Z = (M - M.mean(0)) / np.where(M.std(0) > 0, M.std(0), 1)
    lab, C, inertia = _kmeans(Z, int(k), seed)
    profiles = []
    for j in range(int(k)):
        idx = np.where(lab == j)[0]
        if not len(idx):
            continue
        dev = Z[idx].mean(0)
        order = np.argsort(dev)
        profiles.append({"cluster": j, "size": int(len(idx)),
                         "high": [{"factor": keys[i], "z": round(float(dev[i]), 2)} for i in order[::-1][:3]],
                         "low": [{"factor": keys[i], "z": round(float(dev[i]), 2)} for i in order[:3]],
                         "members": [syms[i] for i in idx][:50]})
    out = {"as_of": as_of, "k": int(k), "factors": keys, "stocks": len(syms), "inertia": round(inertia, 3),
           "clusters": profiles, "method": "kmeans++ on standardised factor scores"}
    if store:
        rid = "CL" + datetime.now().strftime("%Y%m%d%H%M%S")
        conn.execute("INSERT INTO ml_cluster_run (run_id,as_of,method,k,summary_json,created_at) VALUES (?,?,?,?,?,?)",
                     (rid, as_of, "kmeans_factors", int(k), json.dumps(out), datetime.now()))
        conn.executemany("INSERT INTO ml_cluster (run_id,as_of,symbol,cluster) VALUES (?,?,?,?)",
                         [(rid, as_of, s, int(lab[i])) for i, s in enumerate(syms)])
        conn.commit()
        out["run_id"] = rid
    return out


def return_pca(conn, as_of=None, lookback: int = 120, n: int = 5, symbols=None) -> dict:
    as_of = str(as_of or conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50'").fetchone()[0])[:10]
    days = [str(r[0])[:10] for r in conn.execute("SELECT date FROM prices_daily WHERE symbol='NIFTY50' AND date<=? "
                                                 "ORDER BY date DESC LIMIT ?", (as_of, int(lookback) + 1))][::-1]
    if symbols is None:
        from data.dhan import get_tracked_symbols
        symbols = get_tracked_symbols(conn)
    closes = {}
    for sym, d, c in conn.execute(f"SELECT symbol, date, close FROM prices_daily WHERE date>=? AND date<=? AND "
                                  f"symbol IN ({','.join('?' * len(symbols))})", (days[0], as_of, *symbols)):
        closes.setdefault(sym, {})[str(d)[:10]] = float(c)
    syms = [s for s in symbols if len(closes.get(s, {})) == len(days)]
    if len(syms) < n + 5:
        raise ValueError("too few stocks with complete history in the window")
    P = np.array([[closes[s][d] for d in days] for s in syms]).T
    R = np.diff(np.log(P), axis=0)
    Z = (R - R.mean(0)) / np.where(R.std(0) > 0, R.std(0), 1)
    U, S, Vt = np.linalg.svd(Z, full_matrices=False)
    var = S ** 2 / (S ** 2).sum()
    comps = []
    for i in range(min(int(n), len(var))):
        o = np.argsort(-np.abs(Vt[i]))[:8]
        comps.append({"component": i + 1, "explained_variance": round(float(var[i]), 4),
                      "top_loadings": [{"symbol": syms[j], "loading": round(float(Vt[i][j]), 3)} for j in o]})
    return {"as_of": as_of, "sessions": len(days) - 1, "stocks": len(syms), "components": comps,
            "first_component_share": round(float(var[0]), 4),
            "note": "a high first-component share means one common (market) factor drives most moves"}


def run_scheduled_anomalies(trade_date=None) -> dict:
    """Post-market: SKIPPED unless config ml.anomalies_enabled."""
    from db.schema import get_connection
    from ml.config import settings
    if not settings()["anomalies_enabled"]:
        return {"status": "SKIPPED", "rows": 0, "reason": "ml.anomalies_enabled is false"}
    conn = get_connection()
    try:
        r = detect_anomalies(conn, trade_date)
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": r["flagged"], "scanned": r["scanned"]}


def _robust_z(hist, x):
    h = np.asarray([v for v in hist if v is not None and np.isfinite(v)], float)
    if len(h) < 20 or x is None:
        return None
    med = np.median(h)
    mad = np.median(np.abs(h - med)) * 1.4826
    return None if mad <= 0 else float((x - med) / mad)


def detect_anomalies(conn, as_of=None, symbols=None, threshold: float = 4.0, store: bool = True) -> dict:
    as_of = str(as_of or conn.execute("SELECT MAX(date) FROM prices_daily WHERE symbol='NIFTY50'").fetchone()[0])[:10]
    if symbols is None:
        from data.dhan import get_tracked_symbols
        symbols = get_tracked_symbols(conn)
    ca = {r[0] for r in conn.execute("SELECT symbol FROM corporate_actions WHERE ex_date=?", (as_of,))}
    flagged, scanned = [], 0
    for sym in symbols:
        rows = conn.execute("SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol=? AND date<=? "
                            "ORDER BY date DESC LIMIT 62", (sym, as_of)).fetchall()
        if len(rows) < 30 or str(rows[0][0])[:10] != as_of:
            continue
        rows = rows[::-1]
        scanned += 1
        c = [float(r[4]) for r in rows]
        ret = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
        vol = [float(r[5] or 0) for r in rows][1:]
        rng = [(float(r[2]) - float(r[3])) / float(r[4]) if r[4] else None for r in rows][1:]
        gap = [float(rows[i][1]) / c[i - 1] - 1 if rows[i][1] else None for i in range(1, len(rows))]
        z = {"return": _robust_z(ret[:-1], ret[-1]), "volume": _robust_z(np.log1p(vol[:-1]), np.log1p(vol[-1])),
             "range": _robust_z(rng[:-1], rng[-1]), "gap": _robust_z(gap[:-1], gap[-1])}
        zz = [v for v in z.values() if v is not None]
        multi = float(sum(v * v for v in zz))
        top = max(z, key=lambda k_: abs(z[k_]) if z[k_] is not None else -1)
        if zz and (max(abs(v) for v in zz) >= threshold or multi >= threshold ** 2):
            flagged.append({"symbol": sym, "kind": top, "z": {k_: None if v is None else round(v, 2) for k_, v in z.items()},
                            "score": round(multi, 2), "return_pct": round(ret[-1] * 100, 2),
                            "explained": "corporate action ex-date" if sym in ca else None})
    flagged.sort(key=lambda a: -a["score"])
    if store:
        conn.execute("DELETE FROM ml_anomaly WHERE as_of=?", (as_of,))
        conn.executemany("INSERT INTO ml_anomaly (as_of,symbol,kind,score,detail_json,created_at) VALUES (?,?,?,?,?,?)",
                         [(as_of, a["symbol"], a["kind"], a["score"], json.dumps(a), datetime.now()) for a in flagged])
        conn.commit()
    return {"as_of": as_of, "scanned": scanned, "flagged": len(flagged), "threshold": threshold, "anomalies": flagged}
