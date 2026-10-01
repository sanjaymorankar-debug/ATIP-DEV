"""
News novelty, decay and source weighting (W28b: NS-05), on top of the W28 classifier
(data/news_ai.py -- its categories, sentiment and confidence are used as they are).

Per article, before it is stored (enrich):
    novelty       1 - the highest word-shingle Jaccard similarity to any article stored in
                  the previous NOVELTY_LOOKBACK_H hours (or earlier in the same batch);
                  similarity >= DUP_THRESHOLD records dup_of = that article's id
    half_life_h   by category (HALF_LIFE_H): results and M&A matter for days, a flows or
                  global-markets headline for hours
    source_weight news_source_quality.weight when measured, else the feed's configured weight

Per source (refresh_source_quality, post-market): duplicate share, share attributed to a
stock, and -- for articles with a clear sentiment on a stock -- how often the next
session's move had the same sign. weight = configured x (1 - 0.5 x duplicate share),
times (0.5 + hit rate) once there are >= MIN_REACTIONS reactions; clamped to 0.2..1.5.

Per stock (symbol_score / refresh_symbol_scores):
    w_i   = importance_i x source_weight_i x novelty_i x confidence_i x 0.5^(age_i / half_life_i)
    score = 50 + 50 x sum(w_i x sentiment_i) / (sum(w_i) + SHRINK)
over the last SCORE_WINDOW_DAYS days up to the end of the scoring day (point in time).
Duplicates are skipped (the original already counts). SHRINK pulls thin evidence toward
neutral 50: one weak headline barely moves a stock. scores/engine.get_ns reads
news_symbol_score first.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

NOVELTY_LOOKBACK_H = 72
DUP_THRESHOLD = 0.6
SHRINK = 0.5
SCORE_WINDOW_DAYS = 7
MIN_REACTIONS = 20
IMPORTANCE_W = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}
# hours, keyed by data/news_ai.CATEGORIES (anything else: DEFAULT_HALF_LIFE_H)
HALF_LIFE_H = {"EARNINGS": 96, "ORDERS_CONTRACTS": 72, "M_AND_A": 120, "CORPORATE_ACTION": 72, "MANAGEMENT": 72,
               "RATING_TARGET": 48, "REGULATORY_LEGAL": 120, "RBI_POLICY": 48, "GOVT_POLICY": 72, "MACRO": 48,
               "GLOBAL": 18, "GEOPOLITICS": 48, "COMMODITIES": 24, "SECTOR": 48, "FLOWS": 24, "IPO": 48,
               "GENERAL": 24}
DEFAULT_HALF_LIFE_H = 24


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9][a-z0-9&'-]*", (text or "").lower()) if len(t) > 2]


def shingles(text, k=2) -> set:
    w = _tokens(text)
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))} if w else set()


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def configured_weights() -> dict:
    from data.news import RSS_FEEDS
    return {f["name"]: float(f.get("weight", 1.0)) for f in RSS_FEEDS}


def source_weights(conn) -> dict:
    w = configured_weights()
    try:
        for src, wt in conn.execute("SELECT source, weight FROM news_source_quality WHERE weight IS NOT NULL"):
            w[src] = float(wt)
    except Exception:
        pass
    return w


def enrich(articles: list, conn) -> list:
    since = (datetime.now() - timedelta(hours=NOVELTY_LOOKBACK_H)).strftime("%Y-%m-%d %H:%M:%S")
    prior = [(r[0], shingles(r[1])) for r in conn.execute("SELECT id, headline FROM news_articles WHERE fetched_at>=?",
                                                         (since,))]
    sw = source_weights(conn)
    batch = []
    for a in articles:
        sh = shingles(a["headline"])
        best, best_id = 0.0, None
        for pid, psh in prior:
            j = jaccard(sh, psh)
            if j > best:
                best, best_id = j, pid
        for bsh in batch:
            best = max(best, jaccard(sh, bsh))
        batch.append(sh)
        a["novelty"] = round(1.0 - best, 3)
        a["dup_of"] = best_id if best >= DUP_THRESHOLD else None
        a["half_life_h"] = HALF_LIFE_H.get(a.get("category") or "GENERAL", DEFAULT_HALF_LIFE_H)
        a["source_weight"] = sw.get(a.get("source"), 0.8)
    return articles


def _as_dt(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def article_weight(r: dict, now: datetime) -> float:
    pub = _as_dt(r.get("published_at") or r.get("fetched_at"))
    if pub is None:
        return 0.0
    age_h = max(0.0, (now - pub).total_seconds() / 3600)
    hl = r.get("half_life_h") or HALF_LIFE_H.get(r.get("category") or "GENERAL", DEFAULT_HALF_LIFE_H)
    nov = 1.0 if r.get("novelty") is None else max(0.05, float(r["novelty"]))
    srcw = 0.8 if r.get("source_weight") is None else float(r["source_weight"])
    conf = 0.5 if r.get("confidence") is None else float(r["confidence"])
    return IMPORTANCE_W.get(r.get("importance"), 0.7) * srcw * nov * max(conf, 0.05) * 0.5 ** (age_h / hl)


def symbol_score(conn, symbol: str, as_of=None) -> dict | None:
    day = datetime.strptime(str(as_of or date.today())[:10], "%Y-%m-%d")
    end = day + timedelta(days=1)
    since = day - timedelta(days=SCORE_WINDOW_DAYS)
    rows = conn.execute(
        "SELECT sentiment, importance, confidence, category, novelty, source_weight, half_life_h, fetched_at, "
        "published_at, dup_of FROM news_articles WHERE symbols_mentioned LIKE ? AND fetched_at>=? AND fetched_at<?",
        (f'%"{symbol}"%', since.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))).fetchall()
    if not rows:
        return None
    now = min(end, datetime.now())
    sw = swx = 0.0
    n = 0
    for r in rows:
        r = dict(r)
        if r["dup_of"] is not None:
            continue
        w = article_weight(r, now)
        sw += w
        swx += w * float(r["sentiment"] or 0.0)
        n += 1
    if not n:
        return None
    score = 50.0 + 50.0 * swx / (sw + SHRINK)
    return {"score": round(max(0.0, min(100.0, score)), 2), "n_articles": n, "effective_weight": round(sw, 4),
            "mean_sentiment": round(swx / sw, 4) if sw else 0.0}


def refresh_symbol_scores(as_of=None, conn=None) -> dict:
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        td = str(as_of or date.today())[:10]
        since = (datetime.strptime(td, "%Y-%m-%d") - timedelta(days=SCORE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        syms = set()
        for (sm,) in conn.execute("SELECT symbols_mentioned FROM news_articles WHERE fetched_at>=? AND fetched_at<? "
                                  "AND symbols_mentioned NOT IN ('','[]')",
                                  (since, (datetime.strptime(td, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))):
            try:
                syms.update(json.loads(sm or "[]"))
            except ValueError:
                continue
        n = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for s in sorted(syms):
            r = symbol_score(conn, s, td)
            if r is None:
                continue
            conn.execute("INSERT OR REPLACE INTO news_symbol_score (symbol,date,score,n_articles,effective_weight,"
                         "mean_sentiment,components_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (s, td, r["score"], r["n_articles"], r["effective_weight"], r["mean_sentiment"],
                          json.dumps({"window_days": SCORE_WINDOW_DAYS, "shrink": SHRINK}), now))
            n += 1
        conn.commit()
        return {"status": "SUCCESS" if n else "EMPTY", "rows": n, "date": td}
    finally:
        if own:
            conn.close()


def refresh_source_quality(days=30, conn=None) -> dict:
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        cfgw = configured_weights()
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        out = {}
        for (src,) in conn.execute("SELECT DISTINCT source FROM news_articles WHERE fetched_at>=?", (since,)).fetchall():
            rows = conn.execute("SELECT fetched_at, symbols_mentioned, sentiment, dup_of FROM news_articles "
                                "WHERE source=? AND fetched_at>=?", (src, since)).fetchall()
            n = len(rows)
            if not n:
                continue
            dup = sum(1 for r in rows if r[3] is not None) / n
            with_sym = [r for r in rows if r[1] and r[1] != "[]"]
            hits = tot = 0
            for fa, sm, s, _ in with_sym:
                if s is None or abs(s) < 0.3:
                    continue
                d = str(fa)[:10]
                for sym in json.loads(sm or "[]")[:3]:
                    px = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? ORDER BY date DESC "
                                      "LIMIT 1", (sym, d)).fetchone()
                    nx = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date>? ORDER BY date ASC "
                                      "LIMIT 1", (sym, d)).fetchone()
                    if not px or not nx or not px[0]:
                        continue
                    tot += 1
                    hits += (nx[0] > px[0]) == (s > 0)
            hit_rate = hits / tot if tot else None
            base = cfgw.get(src, 0.8) * (1 - 0.5 * dup)
            weight = base * (0.5 + hit_rate) if (hit_rate is not None and tot >= MIN_REACTIONS) else base
            weight = round(max(0.2, min(1.5, weight)), 3)
            conn.execute("INSERT OR REPLACE INTO news_source_quality (source,articles,duplicate_share,symbol_share,"
                         "reaction_hit_rate,reaction_n,configured_weight,weight,detail_json,updated_at) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?)",
                         (src, n, round(dup, 4), round(len(with_sym) / n, 4),
                          None if hit_rate is None else round(hit_rate, 4), tot, cfgw.get(src), weight,
                          json.dumps({"days": days}), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            out[src] = weight
        conn.commit()
        return {"status": "SUCCESS" if out else "EMPTY", "sources": out}
    finally:
        if own:
            conn.close()


def run_scheduled(trade_date=None) -> dict:
    """Post-market, before scoring: source weights, then per-symbol scores for the session."""
    q = refresh_source_quality()
    s = refresh_symbol_scores(trade_date)
    return {"status": "SUCCESS", "rows": s.get("rows", 0), "sources": len(q.get("sources", {}))}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="News novelty / decay / source weighting (NS-05)")
    ap.add_argument("--date")
    ap.add_argument("--sources-only", action="store_true")
    a = ap.parse_args()
    print(refresh_source_quality() if a.sources_only else run_scheduled(a.date))
