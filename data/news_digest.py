"""
ATIP -- AI news summary (W28: NS-04, shown by DB-06).

build_digest(session) summarises the stored news of a session window into a short
market brief -- a headline, a paragraph, up to six bullets, the themes, the stocks
named, and an overall tone -- and stores it in news_digest.

    premarket   articles since the previous evening (18:00 the day before)
    midday      articles since 08:00
    close       articles of the trading day (08:00 -> now)

Method: with news_ai.enabled and an Anthropic key, Claude writes it from the top
articles (ranked by the NS-05 weight: importance x source weight x novelty x
confidence, duplicates removed), within the same daily budget as classification
(news_ai_usage, purpose 'digest'). Otherwise, or past the budget, an EXTRACTIVE
digest is built: the highest-weighted distinct headlines as bullets, the tone from
their weighted sentiment, themes from event-type counts. method='claude' or
'extractive' says which, so the panel never passes one off as the other.

The brief restates what the articles say. It is not advice and is not an input to
any score.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

MAX_ARTICLES = 40
SESSIONS = ("premarket", "midday", "close")

DIGEST_SYSTEM = (
    "You write a short, neutral briefing on the Indian equity market from a list of news headlines with "
    "their source, event type and a sentiment label. Use only what the headlines state; do not add facts, "
    "forecasts, price targets or recommendations. Name NSE stocks only when a headline names them. "
    "Return: headline (one line, at most 90 characters), summary (2-4 sentences), bullets (3-6 items, each at "
    "most 140 characters, most market-moving first), themes (2-5 short labels), symbols (NSE symbols "
    "mentioned, may be empty) and market_tone (BULLISH, BEARISH, MIXED or NEUTRAL)."
)
DIGEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "summary", "bullets", "themes", "symbols", "market_tone"],
    "properties": {
        "headline": {"type": "string"}, "summary": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "themes": {"type": "array", "items": {"type": "string"}},
        "symbols": {"type": "array", "items": {"type": "string"}},
        "market_tone": {"type": "string", "enum": ["BULLISH", "BEARISH", "MIXED", "NEUTRAL"]},
    },
}


def window(session: str, now=None):
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if session == "premarket":
        return today - timedelta(hours=6), now
    if session in ("midday", "close"):
        return today + timedelta(hours=8), now
    raise ValueError(f"session must be one of {SESSIONS}")


def ranked_articles(conn, start, end, limit=MAX_ARTICLES) -> list:
    from data.news_ai import _article_weight
    rows = [dict(r) for r in conn.execute(
        "SELECT id, fetched_at, published_at, headline, source, category, event_type, symbols_mentioned, sentiment, "
        "importance, confidence, novelty, source_weight, half_life_h, dup_of, ai_summary FROM news_articles "
        "WHERE fetched_at>=? AND fetched_at<=?", (start.strftime("%Y-%m-%d %H:%M:%S"),
                                                 end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()]
    rows = [r for r in rows if r.get("dup_of") is None]
    for r in rows:
        r["w"] = _article_weight(r, end)
    rows.sort(key=lambda r: r["w"], reverse=True)
    return rows[:limit]


def _tone(arts) -> str:
    sw = sum(a["w"] for a in arts) or 0.0
    if not sw:
        return "NEUTRAL"
    m = sum(a["w"] * float(a["sentiment"] or 0) for a in arts) / sw
    pos = sum(1 for a in arts if (a["sentiment"] or 0) >= 0.3)
    neg = sum(1 for a in arts if (a["sentiment"] or 0) <= -0.3)
    if pos and neg and min(pos, neg) / max(pos, neg) > 0.6:
        return "MIXED"
    return "BULLISH" if m >= 0.15 else ("BEARISH" if m <= -0.15 else "NEUTRAL")


def extractive(arts) -> dict:
    top = arts[:6]
    ev = Counter((a.get("event_type") or a.get("category") or "OTHER") for a in arts)
    syms = Counter()
    for a in arts:
        try:
            syms.update(json.loads(a.get("symbols_mentioned") or "[]"))
        except ValueError:
            pass
    tone = _tone(arts)
    return {
        "headline": (top[0]["headline"][:90] if top else "No market news in this window"),
        "summary": (f"{len(arts)} distinct stories; overall tone {tone.lower()} by weighted sentiment. "
                    f"Most frequent themes: {', '.join(k for k, _ in ev.most_common(3)) or 'none'}.") if arts else
                   "No stored articles in this window.",
        "bullets": [f"{a['headline'][:130]} ({a['source']})" for a in top],
        "themes": [k.replace("_", " ").title() for k, _ in ev.most_common(4)],
        "symbols": [s for s, _ in syms.most_common(10)],
        "market_tone": tone,
    }


def build_digest(session="close", conn=None, now=None, force_extractive=False) -> dict:
    from data import news_ai
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        start, end = window(session, now)
        arts = ranked_articles(conn, start, end)
        cfg = news_ai.settings()
        out, method, model, status, err = None, "extractive", None, "OK", None
        if arts and not force_extractive and cfg["enabled"] and \
                news_ai.spent_today(conn) < float(cfg["daily_budget_usd"]):
            client = news_ai.get_client()
            if client is not None:
                import anthropic
                lines = "\n".join(
                    f"- [{a['source']}] [{a.get('event_type') or a.get('category')}] "
                    f"[sentiment {float(a['sentiment'] or 0):+.2f}] {a['headline']}" for a in arts)
                try:
                    data, resp, refused = news_ai.call_structured(
                        client, cfg, DIGEST_SYSTEM,
                        f"Session: {session}, {end:%Y-%m-%d %H:%M} IST. Headlines, highest weight first:\n{lines}",
                        DIGEST_SCHEMA, max_tokens=3000)
                    news_ai.record_usage(conn, "digest", cfg["model"], getattr(resp, "usage", None), len(arts),
                                         refusal=refused)
                    if data:
                        out, method, model = data, "claude", getattr(resp, "model", cfg["model"])
                    elif refused:
                        err = "model declined; extractive digest stored"
                except anthropic.APIError as e:
                    news_ai.record_usage(conn, "digest", cfg["model"], error=True)
                    err = f"{e.__class__.__name__}; extractive digest stored"
        if out is None:
            out = extractive(arts)
            if not arts:
                status = "EMPTY"
        did = uuid.uuid4().hex[:16]
        conn.execute(
            "INSERT INTO news_digest (digest_id,date,session,generated_at,method,model,headline,summary,bullets_json,"
            "themes_json,symbols_json,market_tone,articles_used,article_ids_json,status,error) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, str(end.date()), session, end.strftime("%Y-%m-%d %H:%M:%S"), method, model, out["headline"][:200],
             out["summary"][:2000], json.dumps(out["bullets"][:8]), json.dumps(out["themes"][:6]),
             json.dumps(out["symbols"][:20]), out["market_tone"], len(arts), json.dumps([a["id"] for a in arts]),
             status, err))
        conn.commit()
        return {"status": status, "digest_id": did, "method": method, "articles": len(arts), "note": err}
    finally:
        if own:
            conn.close()


def latest(conn, day=None) -> dict | None:
    sql = "SELECT * FROM news_digest"
    args = []
    if day:
        sql += " WHERE date=?"
        args.append(str(day))
    r = conn.execute(sql + " ORDER BY generated_at DESC LIMIT 1", args).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("bullets_json", "themes_json", "symbols_json"):
        d[k[:-5]] = json.loads(d.pop(k) or "[]")
    d.pop("article_ids_json", None)
    return d


def run_scheduled(session="close") -> dict:
    return build_digest(session)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="AI news summary (NS-04)")
    ap.add_argument("--session", default="close", choices=SESSIONS)
    ap.add_argument("--extractive", action="store_true")
    a = ap.parse_args()
    print(build_digest(a.session, force_extractive=a.extractive))
