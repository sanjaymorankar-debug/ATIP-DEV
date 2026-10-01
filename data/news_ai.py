"""
ATIP -- news intelligence (W28: NS-02 classification, NS-03 sentiment + confidence,
NS-05 novelty / decay / source weighting).

Classification (NS-02)
    classify(articles) labels each article with sentiment (-1..1), confidence (0..1),
    importance, category and an EVENT TYPE from a fixed taxonomy (EVENT_TYPES), plus a
    one-line summary. With an Anthropic key and news_ai.enabled, headlines go to Claude
    in batches (one request per BATCH_SIZE headlines, structured JSON output, a frozen
    system prompt so it caches) under a DAILY COST CAP: news_ai_usage keeps calls,
    tokens and estimated USD per day, and once the day's spend reaches
    news_ai.daily_budget_usd every further article is classified by the lexicon model
    instead. Nothing is ever left unclassified.

Sentiment model (NS-03)
    lexicon_sentiment(text): a finance lexicon with negation and intensifiers. Its
    confidence is DERIVED from the evidence -- how many sentiment terms matched and
    how one-sided they were -- not a constant. Stored as sentiment_lex /
    lex_confidence beside every article. When Claude labels an article too, a strong
    disagreement in sign between the two lowers the stored confidence
    (DISAGREE_PENALTY), so confidence reflects agreement, not just the model's
    self-report. Articles labelled only by the lexicon carry classifier='lexicon' and,
    like the old 'rule' fallback, stay out of ACS NewsConfidence (that reads
    classifier='claude' only).

Novelty, decay and source weighting (NS-05)
    novelty      1 - the highest word-shingle Jaccard similarity to any article in the
                 previous NOVELTY_LOOKBACK_H hours (and earlier in the same batch);
                 >= DUP_THRESHOLD similarity marks it dup_of that article.
    half_life_h  per event type (HALF_LIFE_H): results news matters for days, a
                 market wrap for hours.
    source_weight news_source_quality.weight when measured (refresh_source_quality),
                 else the feed's configured weight.
    symbol_score(conn, symbol, as_of) aggregates a stock's last 7 days of news:
        w_i = importance_i * source_weight_i * novelty_i * confidence_i * 0.5^(age_i / half_life_i)
        score = 50 + 50 * sum(w_i * s_i) / (sum(w_i) + SHRINK)
    SHRINK pulls thin evidence toward neutral: one weak article barely moves it.
    refresh_symbol_scores(as_of) stores them in news_symbol_score; scores/engine.get_ns
    reads that table first.

Config (atip_data/config.json, all optional):
    "news_ai": {"enabled": false, "model": "claude-opus-5-5", "effort": "low",
                "daily_budget_usd": 1.0, "batch_size": 20, "max_articles_per_run": 200}
The API key comes from ANTHROPIC_API_KEY (or an `ant auth login` profile); it is never
stored in the database or logged.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path("atip_data") / "config.json"
DEFAULTS = {"enabled": False, "model": "claude-opus-5-5", "effort": "low", "daily_budget_usd": 1.0,
            "batch_size": 20, "max_articles_per_run": 200}
# USD per million tokens (input, output, cache read) -- for the cost cap only; the bill is the authority.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20), "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10), "claude-fable-5-1": (10.00, 50.00, 0.25),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

EVENT_TYPES = {
    # event type: (category, half-life hours)
    "EARNINGS_BEAT": ("EARNINGS", 96), "EARNINGS_MISS": ("EARNINGS", 96), "EARNINGS_INLINE": ("EARNINGS", 72),
    "GUIDANCE_UP": ("EARNINGS", 120), "GUIDANCE_DOWN": ("EARNINGS", 120),
    "ORDER_WIN": ("ORDERS", 72), "CAPEX_EXPANSION": ("ORDERS", 96),
    "M_AND_A": ("M&A", 120), "FUNDRAISE": ("M&A", 72), "BUYBACK": ("M&A", 96), "DIVIDEND": ("EARNINGS", 48),
    "MGMT_CHANGE": ("GENERAL", 72), "PROMOTER_ACTIVITY": ("GENERAL", 96), "BLOCK_DEAL": ("GENERAL", 48),
    "RATING_UPGRADE": ("GENERAL", 72), "RATING_DOWNGRADE": ("GENERAL", 96),
    "BROKER_UPGRADE": ("GENERAL", 48), "BROKER_DOWNGRADE": ("GENERAL", 48),
    "REGULATORY_ACTION": ("GOVT", 120), "LEGAL": ("GENERAL", 96),
    "RBI_POLICY": ("RBI", 48), "GOVT_POLICY": ("GOVT", 72), "GLOBAL_MACRO": ("GLOBAL", 24),
    "GEOPOLITICS": ("GEOPOLITICS", 48), "COMMODITY": ("SECTOR", 24), "SECTOR_TREND": ("SECTOR", 48),
    "IPO": ("GENERAL", 48), "MARKET_WRAP": ("GENERAL", 8), "OTHER": ("GENERAL", 24),
}
CATEGORIES = sorted({c for c, _ in EVENT_TYPES.values()})
IMPORTANCE_W = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}
NOVELTY_LOOKBACK_H = 72
DUP_THRESHOLD = 0.6
DISAGREE_PENALTY = 0.25
SHRINK = 0.5
SCORE_WINDOW_DAYS = 7

SYSTEM_PROMPT = (
    "You classify Indian equity-market news headlines for a quantitative research system. "
    "For each numbered headline return one object with:\n"
    "- sentiment: -1.0 (clearly bad for the company or market named) to 1.0 (clearly good); 0 when neutral "
    "or purely factual.\n"
    "- confidence: 0.0-1.0, how sure you are of the sentiment from the headline alone. Use low values when "
    "the headline is ambiguous, speculative, or the direction depends on facts not stated.\n"
    "- importance: HIGH (likely to move the stock or index the same day), MEDIUM, or LOW.\n"
    f"- event_type: exactly one of {', '.join(EVENT_TYPES)}. Use OTHER when none fits.\n"
    "- summary: at most 120 characters, plain factual restatement, no advice.\n"
    "Judge only what the headline states. Do not infer price targets or recommend trades."
)


def settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("news_ai") or {}
    except Exception:
        raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["enabled"] = out.get("enabled") is True
    return out


# ── lexicon sentiment (NS-03) ─────────────────────────────────────────────
POS = {
    "beat": 1.0, "beats": 1.0, "surge": 1.0, "surges": 1.0, "soar": 1.0, "soars": 1.0, "jump": 0.8, "jumps": 0.8,
    "rally": 0.8, "rallies": 0.8, "gain": 0.6, "gains": 0.6, "rise": 0.5, "rises": 0.5, "up": 0.2,
    "profit": 0.5, "growth": 0.6, "record": 0.7, "upgrade": 0.9, "upgrades": 0.9, "upgraded": 0.9,
    "outperform": 0.8, "buy": 0.4, "wins": 0.8, "win": 0.7, "bags": 0.8, "secures": 0.8, "order": 0.4,
    "orders": 0.4, "expansion": 0.5, "expands": 0.5, "approval": 0.6, "approves": 0.5, "dividend": 0.5,
    "buyback": 0.7, "bonus": 0.5, "strong": 0.6, "robust": 0.6, "higher": 0.4, "improves": 0.6,
    "recovery": 0.5, "rebound": 0.6, "boost": 0.6, "acquire": 0.3, "acquires": 0.3, "stake": 0.2,
    "high": 0.3, "highs": 0.5, "positive": 0.6, "optimistic": 0.6, "margin": 0.1,
}
NEG = {
    "miss": 1.0, "misses": 1.0, "plunge": 1.0, "plunges": 1.0, "crash": 1.0, "crashes": 1.0, "slump": 0.9,
    "slumps": 0.9, "tumble": 0.9, "tumbles": 0.9, "fall": 0.6, "falls": 0.6, "drop": 0.6, "drops": 0.6,
    "decline": 0.6, "declines": 0.6, "down": 0.2, "loss": 0.7, "losses": 0.7, "weak": 0.6, "weaker": 0.6,
    "cut": 0.5, "cuts": 0.5, "downgrade": 0.9, "downgrades": 0.9, "downgraded": 0.9, "underperform": 0.8,
    "sell": 0.4, "fraud": 1.0, "probe": 0.7, "raid": 0.8, "penalty": 0.7, "fine": 0.3, "default": 0.9,
    "resigns": 0.6, "resignation": 0.6, "lower": 0.4, "risk": 0.3, "concern": 0.5, "concerns": 0.5,
    "pressure": 0.4, "slowdown": 0.6, "low": 0.3, "lows": 0.5, "negative": 0.6, "warning": 0.6,
    "pledge": 0.4, "ban": 0.8, "halt": 0.6, "delay": 0.5, "delays": 0.5, "lawsuit": 0.7, "sebi": 0.2,
}
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite"}
INTENSIFIERS = {"sharply": 1.5, "steeply": 1.5, "massive": 1.4, "huge": 1.3, "record": 1.2, "big": 1.2,
                "slightly": 0.6, "marginally": 0.5, "modest": 0.7}


def _tokens(text: str) -> list:
    return re.findall(r"[a-z][a-z&'-]*", (text or "").lower())


def lexicon_sentiment(text: str) -> tuple:
    """(sentiment -1..1, confidence 0..1). Confidence grows with matched evidence and
    shrinks when positive and negative evidence are mixed."""
    toks = _tokens(text)
    pos = neg = 0.0
    hits = 0
    for i, t in enumerate(toks):
        w = POS.get(t)
        sign = 1
        if w is None:
            w = NEG.get(t)
            sign = -1
        if w is None:
            continue
        window = toks[max(0, i - 3):i]
        if any(n in NEGATORS for n in window):
            sign = -sign
        mult = next((INTENSIFIERS[x] for x in reversed(window) if x in INTENSIFIERS), 1.0)
        if sign > 0:
            pos += w * mult
        else:
            neg += w * mult
        hits += 1
    if not hits:
        return 0.0, 0.2
    raw = pos - neg
    sent = math.tanh(raw / 1.5)
    one_sided = abs(pos - neg) / (pos + neg)
    conf = min(0.85, 0.25 + 0.12 * min(hits, 4) + 0.2 * one_sided) * (0.6 + 0.4 * one_sided)
    return round(sent, 3), round(conf, 3)


def event_type_rules(text: str) -> str:
    t = (text or "").lower()
    rules = [
        (r"\b(q[1-4]|quarter|results?|net profit|earnings)\b.*\b(beat|above|surge|jump|rise|up)", "EARNINGS_BEAT"),
        (r"\b(q[1-4]|quarter|results?|net profit|earnings)\b.*\b(miss|below|fall|drop|decline|down|loss)", "EARNINGS_MISS"),
        (r"\b(q[1-4]|quarterly results|earnings)\b", "EARNINGS_INLINE"),
        (r"\bguidance\b.*\b(raise|up|higher)", "GUIDANCE_UP"), (r"\bguidance\b.*\b(cut|lower|down)", "GUIDANCE_DOWN"),
        (r"\b(order|contract|bags|wins|secures)\b", "ORDER_WIN"),
        (r"\b(acquire|acquisition|merger|merge|takeover|stake buy)\b", "M_AND_A"),
        (r"\b(qip|rights issue|fund ?rais|ncd|preferential)\b", "FUNDRAISE"), (r"\bbuy ?back\b", "BUYBACK"),
        (r"\bdividend\b", "DIVIDEND"), (r"\b(ceo|md|cfo|chairman|director)\b.*\b(resign|appoint|quit|steps down)", "MGMT_CHANGE"),
        (r"\bpromoter\b", "PROMOTER_ACTIVITY"), (r"\b(block deal|bulk deal)\b", "BLOCK_DEAL"),
        (r"\b(crisil|icra|care ratings|moody|fitch|s&p)\b.*\bupgrade", "RATING_UPGRADE"),
        (r"\b(crisil|icra|care ratings|moody|fitch|s&p)\b.*\bdowngrade", "RATING_DOWNGRADE"),
        (r"\b(upgrade|target price raised|raises target)\b", "BROKER_UPGRADE"), (r"\b(downgrade|cuts target)\b", "BROKER_DOWNGRADE"),
        (r"\b(sebi|penalty|show cause|ban|raid|ed probe|cbi)\b", "REGULATORY_ACTION"),
        (r"\b(court|tribunal|nclt|lawsuit|arbitration)\b", "LEGAL"),
        (r"\b(rbi|repo rate|monetary policy|mpc)\b", "RBI_POLICY"), (r"\b(budget|gst|government|ministry|cabinet)\b", "GOVT_POLICY"),
        (r"\b(fed|us inflation|treasury|wall street|global markets|dollar index)\b", "GLOBAL_MACRO"),
        (r"\b(war|missile|sanction|border|geopolitic)", "GEOPOLITICS"), (r"\b(crude|gold|silver|copper|metal prices)\b", "COMMODITY"),
        (r"\bipo\b", "IPO"), (r"\b(sensex|nifty)\b.*\b(close|end|settle|open)", "MARKET_WRAP"),
        (r"\b(sector|industry|stocks)\b", "SECTOR_TREND"),
    ]
    for pat, ev in rules:
        if re.search(pat, t):
            return ev
    return "OTHER"


def classify_lexicon(art: dict) -> dict:
    text = art["headline"] + " " + (art.get("summary") or "")[:200]
    s, c = lexicon_sentiment(text)
    ev = event_type_rules(art["headline"])
    imp = "HIGH" if ev in ("EARNINGS_BEAT", "EARNINGS_MISS", "M_AND_A", "REGULATORY_ACTION", "RBI_POLICY") else \
        ("LOW" if ev in ("MARKET_WRAP", "OTHER") else "MEDIUM")
    art.update({"sentiment": s, "confidence": c, "importance": imp, "event_type": ev,
                "category": EVENT_TYPES[ev][0], "ai_summary": art["headline"][:120], "classifier": "lexicon",
                "sentiment_lex": s, "lex_confidence": c, "model": None})
    return art


# ── usage ledger / cost cap (NS-02) ───────────────────────────────────────
def spent_today(conn, purpose=None) -> float:
    sql = "SELECT COALESCE(SUM(cost_usd),0) FROM news_ai_usage WHERE day=?"
    args = [str(date.today())]
    if purpose:
        sql += " AND purpose=?"
        args.append(purpose)
    try:
        return float(conn.execute(sql, args).fetchone()[0])
    except Exception:
        return 0.0


def estimate_cost(model: str, usage) -> float:
    pin, pout, pcache = PRICING.get(model, PRICING["claude-opus-5-5"])
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (inp * pin + cw * pin * 1.25 + cr * pcache + out * pout) / 1e6


def record_usage(conn, purpose, model, usage=None, items=0, refusal=False, error=False):
    cost = estimate_cost(model, usage) if usage is not None else 0.0
    conn.execute(
        "INSERT INTO news_ai_usage (day,purpose,model,calls,items,input_tokens,output_tokens,cache_read_tokens,"
        "cost_usd,refusals,errors,updated_at) VALUES (?,?,?,1,?,?,?,?,?,?,?,?) ON CONFLICT(day,purpose) DO UPDATE SET "
        "model=excluded.model, calls=calls+1, items=items+excluded.items, input_tokens=input_tokens+excluded.input_tokens,"
        "output_tokens=output_tokens+excluded.output_tokens, cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,"
        "cost_usd=cost_usd+excluded.cost_usd, refusals=refusals+excluded.refusals, errors=errors+excluded.errors,"
        "updated_at=excluded.updated_at",
        (str(date.today()), purpose, model, items, getattr(usage, "input_tokens", 0) or 0,
         getattr(usage, "output_tokens", 0) or 0, getattr(usage, "cache_read_input_tokens", 0) or 0,
         round(cost, 6), int(refusal), int(error), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return cost


def get_client():
    """An Anthropic client, or None when the SDK or credentials are missing."""
    try:
        import anthropic
    except ImportError:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
    try:
        return anthropic.Anthropic()
    except Exception as e:
        log.info(f"  news AI: no Anthropic credentials ({e})")
        return None


def call_structured(client, cfg, system, user_text, schema, max_tokens=4000):
    """One Messages call with structured JSON output and server-side refusal fallback.
    Returns (parsed or None, response, refusal: bool). API errors propagate to the caller."""
    resp = client.beta.messages.create(
        model=cfg["model"], max_tokens=max_tokens,
        betas=[FALLBACK_BETA], fallbacks="default",
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": cfg.get("effort") or "low",
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_text}],
    )
    if resp.stop_reason == "refusal":
        return None, resp, True
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        return None, resp, False
    try:
        return json.loads(text), resp, False
    except ValueError:
        return None, resp, False


BATCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "sentiment", "confidence", "importance", "event_type", "summary"],
        "properties": {
            "id": {"type": "integer"}, "sentiment": {"type": "number"}, "confidence": {"type": "number"},
            "importance": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
            "summary": {"type": "string"},
        }}}},
}


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def classify(articles: list, conn=None) -> list:
    """Label every article. Claude within the day's budget, the lexicon for the rest."""
    for a in articles:                                   # the lexicon runs on everything: it is the
        classify_lexicon(a)                              # fallback and the agreement check
    cfg = settings()
    if not articles or not cfg["enabled"]:
        return articles
    client = get_client()
    if client is None:
        return articles
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        import anthropic
        todo = articles[: int(cfg["max_articles_per_run"])]
        bs = max(1, int(cfg["batch_size"]))
        for i in range(0, len(todo), bs):
            if spent_today(conn) >= float(cfg["daily_budget_usd"]):
                log.warning(f"  news AI: daily budget ${cfg['daily_budget_usd']} reached -- "
                            f"{len(todo) - i} articles stay lexicon-classified")
                break
            batch = todo[i:i + bs]
            lines = "\n".join(f"{j}. [{a.get('source', '')}] {a['headline']}" for j, a in enumerate(batch))
            try:
                data, resp, refused = call_structured(client, cfg, SYSTEM_PROMPT,
                                                      f"Classify these headlines:\n{lines}", BATCH_SCHEMA)
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                log.warning(f"  news AI: transient error, rest of run stays lexicon ({e.__class__.__name__})")
                record_usage(conn, "classify", cfg["model"], error=True)
                break
            except anthropic.APIStatusError as e:
                log.warning(f"  news AI: API error {e.status_code}; lexicon for this run")
                record_usage(conn, "classify", cfg["model"], error=True)
                break
            record_usage(conn, "classify", cfg["model"], getattr(resp, "usage", None), len(batch), refusal=refused)
            for it in (data or {}).get("items", []):
                j = it.get("id")
                if not isinstance(j, int) or not 0 <= j < len(batch):
                    continue
                a = batch[j]
                ev = it.get("event_type") if it.get("event_type") in EVENT_TYPES else "OTHER"
                s = _clamp(it.get("sentiment"), -1, 1, 0.0)
                c = _clamp(it.get("confidence"), 0, 1, 0.5)
                lex, lc = a["sentiment_lex"], a["lex_confidence"]
                if abs(s) >= 0.3 and abs(lex) >= 0.3 and (s > 0) != (lex > 0) and lc >= 0.5:
                    c = max(0.0, c - DISAGREE_PENALTY)
                a.update({"sentiment": round(s, 3), "confidence": round(c, 3), "importance": it.get("importance", "MEDIUM"),
                          "event_type": ev, "category": EVENT_TYPES[ev][0],
                          "ai_summary": (it.get("summary") or a["headline"])[:160],
                          "classifier": "claude", "model": getattr(resp, "model", cfg["model"])})
    finally:
        if own:
            conn.close()
    return articles


# ── novelty / decay / source weight (NS-05) ───────────────────────────────
def _shingles(text: str, k=2) -> set:
    w = [t for t in _tokens(text) if len(t) > 2]
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))} if w else set()


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def source_weights(conn) -> dict:
    from data.news import RSS_FEEDS
    w = {f["name"]: float(f.get("weight", 1.0)) for f in RSS_FEEDS}
    try:
        for src, wt in conn.execute("SELECT source, weight FROM news_source_quality WHERE weight IS NOT NULL"):
            w[src] = float(wt)
    except Exception:
        pass
    return w


def enrich(articles: list, conn) -> list:
    """novelty / dup_of / half-life / source weight, in place, before storing."""
    since = datetime.now() - timedelta(hours=NOVELTY_LOOKBACK_H)
    prior = [(r[0], _shingles(r[1])) for r in conn.execute(
        "SELECT id, headline FROM news_articles WHERE fetched_at>=?", (since.strftime("%Y-%m-%d %H:%M:%S"),))]
    sw = source_weights(conn)
    seen_batch = []
    for a in articles:
        sh = _shingles(a["headline"])
        best, best_id = 0.0, None
        for pid, psh in prior:
            j = _jaccard(sh, psh)
            if j > best:
                best, best_id = j, pid
        for bsh in seen_batch:
            best = max(best, _jaccard(sh, bsh))
        seen_batch.append(sh)
        a["novelty"] = round(1.0 - best, 3)
        a["dup_of"] = best_id if best >= DUP_THRESHOLD else None
        ev = a.get("event_type") or "OTHER"
        a["half_life_h"] = EVENT_TYPES.get(ev, EVENT_TYPES["OTHER"])[1]
        a["source_weight"] = sw.get(a.get("source"), 0.8)
    return articles


def _article_weight(r, now) -> float:
    pub = r["published_at"] or r["fetched_at"]
    if isinstance(pub, str):
        try:
            pub = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0.0
    age_h = max(0.0, (now - pub).total_seconds() / 3600)
    hl = r["half_life_h"] or 24.0
    nov = 1.0 if r["novelty"] is None else max(0.05, float(r["novelty"]))
    srcw = 0.8 if r["source_weight"] is None else float(r["source_weight"])
    conf = 0.5 if r["confidence"] is None else float(r["confidence"])
    return IMPORTANCE_W.get(r["importance"], 0.7) * srcw * nov * max(conf, 0.05) * 0.5 ** (age_h / hl)


def symbol_score(conn, symbol: str, as_of=None) -> dict | None:
    """The NS-05 score for `symbol` at the end of `as_of` (point in time), or None with no news."""
    day = datetime.strptime(str(as_of or date.today())[:10], "%Y-%m-%d")
    end = day + timedelta(days=1)
    since = day - timedelta(days=SCORE_WINDOW_DAYS)
    rows = conn.execute(
        "SELECT sentiment, importance, confidence, novelty, source_weight, half_life_h, fetched_at, published_at, "
        "dup_of, classifier FROM news_articles WHERE symbols_mentioned LIKE ? AND fetched_at>=? AND fetched_at<?",
        (f'%"{symbol}"%', since.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))).fetchall()
    if not rows:
        return None
    now = min(end, datetime.now()) if day.date() == date.today() else end
    sw = swx = 0.0
    n = 0
    for r in rows:
        r = dict(r)
        if r["dup_of"] is not None:
            continue                                     # the original already counts
        w = _article_weight(r, now)
        sw += w
        swx += w * float(r["sentiment"] or 0.0)
        n += 1
    if n == 0:
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
        for (sm,) in conn.execute("SELECT symbols_mentioned FROM news_articles WHERE fetched_at>=? AND "
                                  "symbols_mentioned NOT IN ('','[]')", (since,)):
            try:
                syms.update(json.loads(sm or "[]"))
            except ValueError:
                continue
        n = 0
        for s in sorted(syms):
            r = symbol_score(conn, s, td)
            if r is None:
                continue
            conn.execute("INSERT OR REPLACE INTO news_symbol_score (symbol,date,score,n_articles,effective_weight,"
                         "mean_sentiment,components_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (s, td, r["score"], r["n_articles"], r["effective_weight"], r["mean_sentiment"],
                          json.dumps({"window_days": SCORE_WINDOW_DAYS, "shrink": SHRINK}),
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            n += 1
        conn.commit()
        return {"status": "SUCCESS", "rows": n, "date": td}
    finally:
        if own:
            conn.close()


def refresh_source_quality(days=30, conn=None) -> dict:
    """Per source: duplicate share, share attributed to a stock, and -- for articles with a
    clear sentiment on a stock -- how often the next session's move had the same sign.
    weight = configured * (1 - 0.5 * duplicate_share) * (0.5 + hit_rate when >= 20 reactions)."""
    own = conn is None
    if own:
        from db.schema import get_connection
        conn = get_connection()
    try:
        from data.news import RSS_FEEDS
        cfgw = {f["name"]: float(f.get("weight", 1.0)) for f in RSS_FEEDS}
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        out = {}
        for src, in conn.execute("SELECT DISTINCT source FROM news_articles WHERE fetched_at>=?", (since,)).fetchall():
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
            weight = base * (0.5 + hit_rate) if (hit_rate is not None and tot >= 20) else base
            weight = round(max(0.2, min(1.5, weight)), 3)
            conn.execute("INSERT OR REPLACE INTO news_source_quality (source,articles,duplicate_share,symbol_share,"
                         "reaction_hit_rate,reaction_n,configured_weight,weight,detail_json,updated_at) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?)",
                         (src, n, round(dup, 4), round(len(with_sym) / n, 4),
                          None if hit_rate is None else round(hit_rate, 4), tot, cfgw.get(src), weight,
                          json.dumps({"days": days}), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            out[src] = weight
        conn.commit()
        return {"status": "SUCCESS", "sources": out}
    finally:
        if own:
            conn.close()


def run_scheduled(trade_date=None) -> dict:
    """Post-market: source quality (weekly cadence is enough, cheap to run daily) + symbol scores."""
    q = refresh_source_quality()
    s = refresh_symbol_scores(trade_date)
    return {"status": "SUCCESS", "sources": len(q.get("sources", {})), "symbol_scores": s.get("rows", 0)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="News intelligence (NS-02/03/05)")
    ap.add_argument("--scores", action="store_true", help="refresh news_symbol_score for --date")
    ap.add_argument("--sources", action="store_true", help="refresh news_source_quality")
    ap.add_argument("--date")
    ap.add_argument("--test", help="classify one headline with the lexicon (and Claude if enabled)")
    a = ap.parse_args()
    if a.test:
        print(classify([{"headline": a.test, "source": "test"}])[0])
    if a.sources:
        print(refresh_source_quality())
    if a.scores:
        print(refresh_symbol_scores(a.date))
