"""
News classification, sentiment and summary (NS-02 / NS-03 / NS-04), W28.

MODEL PATH (owner decision 2026-10-01: Claude Haiku 4.5 with a daily cost cap)
    classify(articles)      headlines go to claude-haiku-4-5 in batches of `batch_size`
                            (one request per batch, not per headline) with a JSON-schema
                            structured output: per headline sentiment (-1..1), confidence
                            (0..1, the model's own certainty), importance, category from
                            the ATIP taxonomy, NSE symbols it names, a one-line rationale.
                            classifier = 'claude' (what ACS's NewsConfidence reads).
    market_summary(conn)    one request over the window's classified headlines -> a market
                            brief: summary, bullets, tone, key risks, stocks in focus with
                            the headline indexes they rest on. Stored in news_summary.

COST CAP
    Every request's usage (input / output / cache tokens) and its cost at the model's
    published rate is written to ai_usage_log. Before each request the day's spend is
    checked against news.daily_cost_cap_usd; at the cap the rest of the day falls back
    to the rule path. Pricing table: PRICES (USD per million tokens).

RULE PATH (always available; used when news.ai_enabled is false, no key, cap reached,
or a request fails)
    classify_rule(art)      a lexicon with negation handling and per-category keyword sets.
                            Confidence is MEASURED from the evidence (matched terms, their
                            agreement), not the old constant 0.5 -- but classifier stays
                            'rule', so ACS still ignores it (scores/engine.get_news_confidence).
    summary_rule(conn)      counts by category / tone and the most-mentioned stocks; labelled
                            as rule-based, never as AI.

CONFIG  config.json "news": {"ai_enabled": false, "model": "claude-haiku-4-5",
        "daily_cost_cap_usd": 1.0, "batch_size": 20, "summary_hours": 24, "summary_max_items": 60}
        API key: ANTHROPIC_API_KEY (.env). Off by default; the key in .env returned 401 on
        2026-09-24 (KNOWN_ISSUES KD-001), so set a working one before enabling.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"ai_enabled": False, "model": "claude-haiku-4-5", "daily_cost_cap_usd": 1.0, "batch_size": 20,
            "summary_hours": 24, "summary_max_items": 60}
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
CACHE_WRITE_X, CACHE_READ_X = 1.25, 0.10

CATEGORIES = ["EARNINGS", "ORDERS_CONTRACTS", "M_AND_A", "CORPORATE_ACTION", "MANAGEMENT", "RATING_TARGET",
              "REGULATORY_LEGAL", "RBI_POLICY", "GOVT_POLICY", "MACRO", "GLOBAL", "GEOPOLITICS", "COMMODITIES",
              "SECTOR", "FLOWS", "IPO", "GENERAL"]
IMPORTANCE = ["LOW", "MEDIUM", "HIGH"]

SYSTEM = (
    "You classify Indian equity-market news headlines for a quantitative scoring system.\n"
    "For each numbered headline return: sentiment from -1 (clearly negative for the stocks or market "
    "named) to 1 (clearly positive), 0 when neutral or unclear; confidence 0..1 = how certain you are "
    "of that sentiment from the headline alone (ambiguous or opinion headlines are low); importance "
    "HIGH only for results, large orders, M&A, regulatory action, RBI / budget / macro surprises, LOW "
    "for opinion, previews, routine updates; one category; the NSE ticker symbols of listed Indian "
    "companies the headline is about (uppercase NSE codes such as RELIANCE, TCS, HDFCBANK; empty when "
    "none or unsure, never guess); a rationale of at most 15 words.\n"
    "Judge only what the headline states. Do not infer price moves that are not stated."
)
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "sentiment": {"type": "number"}, "confidence": {"type": "number"},
                       "importance": {"type": "string", "enum": IMPORTANCE},
                       "category": {"type": "string", "enum": CATEGORIES},
                       "symbols": {"type": "array", "items": {"type": "string"}},
                       "rationale": {"type": "string"}},
        "required": ["i", "sentiment", "confidence", "importance", "category", "symbols", "rationale"],
        "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
SUMMARY_SYSTEM = (
    "You write a concise pre-trade market brief for an Indian equities desk from classified news "
    "headlines. Use only the headlines given; cite the headline numbers each point rests on. "
    "No price targets, no buy/sell recommendations, no facts not present in the headlines."
)
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "tone": {"type": "string", "enum": ["RISK_ON", "MIXED", "RISK_OFF", "QUIET"]},
        "bullets": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "integer"}}},
            "required": ["text", "refs"], "additionalProperties": False}},
        "key_risks": {"type": "array", "items": {"type": "string"}},
        "stocks_in_focus": {"type": "array", "items": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "why": {"type": "string"},
            "refs": {"type": "array", "items": {"type": "integer"}}},
            "required": ["symbol", "why", "refs"], "additionalProperties": False}}},
    "required": ["headline", "tone", "bullets", "key_risks", "stocks_in_focus"], "additionalProperties": False}


# ── settings, cost ledger ──────────────────────────────────────────────────
def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        return {**DEFAULTS, **(cfg.get("news") or {})}
    except Exception:
        return dict(DEFAULTS)


def cost_usd(model, usage) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-haiku-4-5"])
    g = (lambda k: getattr(usage, k, 0) or 0)
    return round((g("input_tokens") * pin + g("cache_creation_input_tokens") * pin * CACHE_WRITE_X +
                  g("cache_read_input_tokens") * pin * CACHE_READ_X + g("output_tokens") * pout) / 1e6, 6)


def spend_today(conn, day=None) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage_log WHERE day=?", (str(day or date.today()),)
                     ).fetchone()
    return float(r[0] or 0)


def _log_usage(conn, purpose, model, usage, ok=True, error=None):
    g = (lambda k: getattr(usage, k, 0) or 0) if usage is not None else (lambda k: 0)
    conn.execute("INSERT INTO ai_usage_log (day,created_at,purpose,model,input_tokens,output_tokens,cache_write_tokens,"
                 "cache_read_tokens,cost_usd,ok,error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (str(date.today()), datetime.now(), purpose, model, g("input_tokens"), g("output_tokens"),
                  g("cache_creation_input_tokens"), g("cache_read_input_tokens"),
                  cost_usd(model, usage) if usage is not None else 0.0, 1 if ok else 0, (str(error)[:300] if error
                                                                                           else None)))
    conn.commit()


def usage_summary(conn, days=7) -> dict:
    since = str(date.today() - timedelta(days=days - 1))
    rows = conn.execute("SELECT day, purpose, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), "
                        "SUM(1-ok) FROM ai_usage_log WHERE day>=? GROUP BY day, purpose ORDER BY day DESC",
                        (since,)).fetchall()
    s = settings()
    return {"enabled": bool(s["ai_enabled"]), "model": s["model"], "daily_cap_usd": s["daily_cost_cap_usd"],
            "spent_today_usd": round(spend_today(conn), 4),
            "by_day": [{"day": r[0], "purpose": r[1], "requests": r[2], "input_tokens": r[3], "output_tokens": r[4],
                        "cost_usd": round(r[5] or 0, 4), "failed": r[6]} for r in rows]}


class _Unavailable(Exception):
    pass


def _client():
    try:
        import anthropic
    except ImportError as e:
        raise _Unavailable("anthropic SDK not installed") from e
    import os
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
    return anthropic.Anthropic(max_retries=2, timeout=60.0)


def _call(conn, purpose, system, user, schema, max_tokens):
    """One structured-output request under the cap. Returns parsed JSON or raises _Unavailable."""
    import anthropic
    s = settings()
    if not s["ai_enabled"]:
        raise _Unavailable("news.ai_enabled is false")
    if spend_today(conn) >= float(s["daily_cost_cap_usd"]):
        raise _Unavailable(f"daily AI cost cap {s['daily_cost_cap_usd']} USD reached")
    model = s["model"]
    try:
        resp = _client().messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}})
    except anthropic.AuthenticationError as e:
        _log_usage(conn, purpose, model, None, ok=False, error=e)
        raise _Unavailable("Anthropic API key rejected (401)") from e
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APIStatusError) as e:
        _log_usage(conn, purpose, model, None, ok=False, error=e)
        raise _Unavailable(f"Anthropic API error: {e}") from e
    _log_usage(conn, purpose, model, resp.usage)
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise _Unavailable(f"stop_reason {resp.stop_reason}")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


# ── rule path (NS-03 fallback) ─────────────────────────────────────────────
POS = {"profit": 1, "growth": 1, "surge": 1.5, "soar": 1.5, "jump": 1.2, "rise": 0.8, "rises": 0.8, "gain": 0.8,
       "gains": 0.8, "rally": 1.2, "record": 1, "beat": 1.2, "beats": 1.2, "upgrade": 1.3, "upgrades": 1.3,
       "order": 0.8, "orders": 0.8, "wins": 1, "win": 0.8, "bags": 1, "secures": 1, "approval": 1, "approves": 0.8,
       "expansion": 0.8, "dividend": 0.6, "bonus": 0.8, "buyback": 1, "strong": 0.8, "outperform": 1.2,
       "high": 0.4, "boost": 1, "recovers": 0.8, "inflows": 0.8, "upbeat": 1}
NEG = {"loss": 1.2, "losses": 1.2, "decline": 1, "declines": 1, "fall": 0.8, "falls": 0.8, "slump": 1.5,
       "plunge": 1.5, "plunges": 1.5, "crash": 1.8, "weak": 0.8, "cut": 0.8, "cuts": 0.8, "miss": 1.2,
       "misses": 1.2, "downgrade": 1.3, "downgrades": 1.3, "fraud": 2, "probe": 1.2, "penalty": 1.2, "fine": 0.8,
       "default": 1.8, "resigns": 1, "raid": 1.5, "ban": 1.2, "sebi order": 1, "outflows": 0.8, "selloff": 1.2,
       "sell-off": 1.2, "lower": 0.5, "slips": 0.8, "drags": 0.8, "pledge": 0.6, "layoffs": 1, "strike": 0.8}
NEGATORS = ("not ", "no ", "fails to ", "despite ", "without ")
CAT_KEYS = [("REGULATORY_LEGAL", ("sebi", "probe", "court", "penalty", "ban", "nclt", "ed raid", "cci")),
            ("EARNINGS", ("q1", "q2", "q3", "q4", "quarter", "results", "profit", "revenue", "earnings", "ebitda")),
            ("ORDERS_CONTRACTS", ("order", "contract", "bags", "wins", "secures", "tender")),
            ("M_AND_A", ("acquire", "acquisition", "merger", "stake", "takeover", "buyout")),
            ("CORPORATE_ACTION", ("dividend", "bonus", "split", "buyback", "rights issue", "record date")),
            ("MANAGEMENT", ("ceo", "md ", "chairman", "resigns", "appoints", "board")),
            ("RATING_TARGET", ("upgrade", "downgrade", "target price", "rating", "brokerage")),
            ("RBI_POLICY", ("rbi", "repo", "monetary policy", "mpc")),
            ("GOVT_POLICY", ("budget", "gst", "government", "ministry", "pli", "cabinet")),
            ("MACRO", ("inflation", "cpi", "gdp", "iip", "pmi", "fiscal")),
            ("GLOBAL", ("fed", "wall street", "us market", "nasdaq", "dow", "china", "global")),
            ("GEOPOLITICS", ("war", "sanction", "tariff", "border", "conflict")),
            ("COMMODITIES", ("crude", "oil", "gold", "silver", "metal", "copper")),
            ("FLOWS", ("fii", "fpi", "dii", "inflow", "outflow")),
            ("IPO", ("ipo", "listing", "subscription")),
            ("SECTOR", ("sector", "banks", "auto", "pharma", "it stocks", "realty", "psu"))]
HIGH_CATS = {"EARNINGS", "M_AND_A", "REGULATORY_LEGAL", "RBI_POLICY"}


def classify_rule(art: dict) -> dict:
    h = " " + art["headline"].lower() + " "
    score, hits = 0.0, 0
    for lex, sign in ((POS, 1), (NEG, -1)):
        for w, wt in lex.items():
            for m in re.finditer(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", h):
                pre = h[max(0, m.start() - 12):m.start()]
                s = -sign if any(n in pre for n in NEGATORS) else sign
                score += s * wt
                hits += 1
    sent = max(-1.0, min(1.0, score / 3.0))
    pos_hits = sum(1 for w in POS if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", h))
    neg_hits = sum(1 for w in NEG if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", h))
    conflict = min(pos_hits, neg_hits)
    # measured: more matched evidence -> more confident; conflicting evidence -> less
    conf = 0.15 if hits == 0 else min(0.85, 0.35 + 0.12 * hits - 0.2 * conflict)
    cat = next((c for c, keys in CAT_KEYS if any(k in h for k in keys)), "GENERAL")
    imp = "HIGH" if (cat in HIGH_CATS and abs(sent) >= 0.4) else "MEDIUM" if (hits or cat != "GENERAL") else "LOW"
    art.update({"sentiment": round(sent, 2), "confidence": round(max(0.05, conf), 2), "importance": imp,
                "category": cat, "ai_summary": art["headline"][:80], "classifier": "rule"})
    return art


# ── model path ─────────────────────────────────────────────────────────────
def _norm_symbols(syms, known):
    out = []
    for s in syms or []:
        s = re.sub(r"[^A-Z0-9&\-]", "", str(s).upper())[:20]
        if s and (not known or s in known) and s not in out:
            out.append(s)
    return out


def classify(articles: list, conn=None) -> list:
    """Classify in place; model path per batch, rule path for whatever the model did not cover."""
    if not articles:
        return articles
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        s = settings()
        known = set()
        try:
            from data.dhan import get_tracked_symbols
            known = set(get_tracked_symbols(conn))
        except Exception:
            pass
        bs = max(1, int(s.get("batch_size") or 20))
        stop_reason = None
        for i in range(0, len(articles), bs):
            batch = articles[i:i + bs]
            if stop_reason is None:
                try:
                    user = "Headlines:\n" + "\n".join(f"{n}. {a['headline'][:300]}" for n, a in enumerate(batch))
                    out = _call(conn, "news_classify", SYSTEM, user, CLASSIFY_SCHEMA, 200 + 120 * len(batch))
                    got = {int(x["i"]): x for x in out.get("items", []) if isinstance(x, dict) and "i" in x}
                    for n, a in enumerate(batch):
                        x = got.get(n)
                        if not x:
                            classify_rule(a)
                            continue
                        a.update({"sentiment": round(max(-1.0, min(1.0, float(x["sentiment"]))), 3),
                                  "confidence": round(max(0.0, min(1.0, float(x["confidence"]))), 3),
                                  "importance": x["importance"] if x["importance"] in IMPORTANCE else "MEDIUM",
                                  "category": x["category"] if x["category"] in CATEGORIES else "GENERAL",
                                  "ai_summary": str(x.get("rationale") or a["headline"])[:160],
                                  "ai_symbols": _norm_symbols(x.get("symbols"), known), "classifier": "claude"})
                    continue
                except _Unavailable as e:
                    stop_reason = str(e)
                    log.info(f"  News AI off for this run ({stop_reason}) — rule-based classification")
                except (ValueError, KeyError, TypeError) as e:
                    log.warning(f"  News AI batch unparseable ({e}) — rule-based for this batch")
            for a in batch:
                classify_rule(a)
        return articles
    finally:
        if own:
            conn.close()


# ── summary (NS-04) ────────────────────────────────────────────────────────
def _window(conn, hours, limit):
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(r) for r in conn.execute(
        "SELECT id, fetched_at, headline, source, category, importance, sentiment, confidence, symbols_mentioned, "
        "classifier FROM news_articles WHERE fetched_at>=? ORDER BY CASE importance WHEN 'HIGH' THEN 0 WHEN "
        "'MEDIUM' THEN 1 ELSE 2 END, fetched_at DESC LIMIT ?", (since, int(limit))).fetchall()]


def summary_rule(arts: list) -> dict:
    by_cat, mentions = {}, {}
    for a in arts:
        cat = a.get("category") or "GENERAL"
        if cat == "GENERAL" and a.get("classifier") != "claude":      # pre-W28 rows: re-derive the category
            cat = classify_rule({"headline": a["headline"]})["category"]
        by_cat[cat] = by_cat.get(cat, 0) + 1
        for s in json.loads(a.get("symbols_mentioned") or "[]"):
            mentions[s] = mentions.get(s, 0) + 1
    sents = [a["sentiment"] for a in arts if a.get("sentiment") is not None]
    avg = sum(sents) / len(sents) if sents else 0
    tone = "QUIET" if len(arts) < 5 else "RISK_ON" if avg > 0.15 else "RISK_OFF" if avg < -0.15 else "MIXED"
    top = sorted(mentions.items(), key=lambda x: -x[1])[:8]
    return {"headline": f"{len(arts)} headlines; average sentiment {avg:+.2f} (rule-based, not AI)", "tone": tone,
            "bullets": [{"text": f"{c}: {n} headlines", "refs": []} for c, n in
                        sorted(by_cat.items(), key=lambda x: -x[1])[:6]],
            "key_risks": [], "stocks_in_focus": [{"symbol": s, "why": f"{n} mentions", "refs": []} for s, n in top]}


def market_summary(conn=None, hours=None) -> dict:
    from db.schema import get_connection
    own = conn is None
    conn = conn or get_connection()
    try:
        s = settings()
        hours = int(hours or s["summary_hours"])
        arts = _window(conn, hours, int(s["summary_max_items"]))
        kind, err = "rule", None
        if not arts:
            out = {"headline": "No news in the window", "tone": "QUIET", "bullets": [], "key_risks": [],
                   "stocks_in_focus": []}
        else:
            try:
                lines = [f"{n}. [{a.get('importance')}/{a.get('category')}/{(a.get('sentiment') or 0):+.2f}] "
                         f"{a['headline'][:240]}" for n, a in enumerate(arts)]
                out = _call(conn, "news_summary", SUMMARY_SYSTEM,
                            f"Window: last {hours} hours. Headlines (importance/category/sentiment):\n" +
                            "\n".join(lines), SUMMARY_SCHEMA, 1500)
                kind = "claude"
            except (_Unavailable, ValueError, KeyError, TypeError) as e:
                err = str(e)
                out = summary_rule(arts)
        refs = {n: a["id"] for n, a in enumerate(arts)}
        for b in out.get("bullets", []) + out.get("stocks_in_focus", []):
            b["article_ids"] = [refs[r] for r in b.get("refs", []) if r in refs]
        conn.execute("INSERT INTO news_summary (created_at,window_hours,article_count,classifier,model,summary_json,"
                     "fallback_reason) VALUES (?,?,?,?,?,?,?)",
                     (datetime.now(), hours, len(arts), kind, s["model"] if kind == "claude" else None,
                      json.dumps(out), err))
        conn.commit()
        return {"classifier": kind, "article_count": len(arts), "window_hours": hours, "fallback_reason": err, **out}
    finally:
        if own:
            conn.close()


def latest_summary(conn) -> dict | None:
    r = conn.execute("SELECT * FROM news_summary ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = dict(r)
    d.update(json.loads(d.pop("summary_json") or "{}"))
    return d


def run_summary_job(hours=None) -> dict:
    r = market_summary(hours=hours)
    return {"status": "SUCCESS", "rows": 1, "classifier": r["classifier"], "articles": r["article_count"]}
