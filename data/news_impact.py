"""
News Impact Estimate (NI-01..NI-05, 2026-10-27): what the day's Indian and world news is likely to do to the
Indian market, and a running check of how often that call was right.

    run(label)              the whole job, end to end, from the feeds:
                              1 fetch     Indian feeds (data/news.RSS_FEEDS) and the world feeds (GLOBAL_FEEDS --
                                          Wall Street / US yields, Asian markets, Pakistan) through the existing
                                          pipeline, so every headline is de-duplicated, classified and stored once
                              2 assess    each market-relevant headline not yet assessed (assess_pending)
                              3 combine   one call for the coming NSE session (aggregate) + a short brief
                              4 track     every call whose session has closed is scored (evaluate)
    assess_pending(conn)    HIGH / MEDIUM headlines of the last `window_hours`, minus repeats of the same story
                            (news_articles.dup_of) and market-recap headlines ("Sensex jumps 483 points" reports a
                            move, it does not cause one). Per headline:
                              india_relevance  0..1, how much it matters to Indian equities at all
                              nifty_direction  UP / DOWN / NONE        magnitude  NONE / SMALL / MEDIUM / LARGE
                              horizon          INTRADAY / NEXT_SESSION / MULTI_DAY
                              transmission     the channel it travels by (FII flows, crude, rupee, rates, ...)
                              sectors          [{sector, direction}] over the NSE sector indices ATIP stores
                              confidence 0..1, rationale (<= 20 words)
                            MODEL PATH: Claude, the news model and daily cost cap of data/news_ai (one request per
                            `batch_size` headlines, JSON-schema output, purpose "news_impact" in ai_usage_log).
                            RULE PATH (no key, cap reached, API down, news.ai_enabled false): the stored sentiment /
                            importance / category; a crude / yield / dollar / inflation move named in the headline
                            ("crude jumps", "oil decline") decides the direction, the other way round for India;
                            company news kept off the Nifty; never LARGE; confidence <= 0.5.
    aggregate(conn)         points = direction x magnitude (SMALL 1, MEDIUM 2, LARGE 4) x confidence x relevance x
                            recency (half-life `half_life_hours`); score = 50 + 50 tanh(net / score_scale);
                            POSITIVE >= positive_at, NEGATIVE <= negative_at, else NEUTRAL; strength WEAK /
                            MODERATE / STRONG by distance from 50. The same per sector. Top drivers by |points|.
    evaluate(conn)          against index_levels (the 15:30 row): Nifty 50 % change of the target session vs the
                            call (a NEUTRAL call is right when |change| <= neutral_band_pct), each sector the same,
                            each headline's direction. Kept in news_impact_eval / news_impact.
    accuracy(conn, days)    hit rate of the last pre-open call per session, by method (claude / rule), next to
                            two naive baselines (always the commonest outcome; yesterday's direction again) -- the
                            estimate is worth reading only where it beats them.

The estimate is INFORMATIONAL: nothing here places, sizes or blocks an order, and it does not feed ATIP scores.
CONFIG  config.json "news_impact" (DEFAULTS below); Claude itself is switched by config.json "news".
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DEFAULTS = {"enabled": True, "window_hours": 18, "max_items": 40, "batch_size": 20, "fetch_feeds": True,
            "fetch_hours": 6, "brief": True, "half_life_hours": 12.0, "score_scale": 4.0, "positive_at": 58,
            "negative_at": 42, "neutral_band_pct": 0.30, "eval_give_up_days": 5,
            "magnitude_points": {"NONE": 0, "SMALL": 1, "MEDIUM": 2, "LARGE": 4},
            "run_times_ist": {"premarket": "07:40", "midday": "12:20", "close": "15:50", "evening": "20:15"},
            "evaluate_time_ist": "16:20"}

DIRECTIONS = ["UP", "DOWN", "NONE"]
MAGNITUDES = ["NONE", "SMALL", "MEDIUM", "LARGE"]
HORIZONS = ["INTRADAY", "NEXT_SESSION", "MULTI_DAY"]
TRANSMISSIONS = ["FII_FLOWS", "CRUDE_COMMODITIES", "CURRENCY", "RATES_LIQUIDITY", "EARNINGS", "GLOBAL_RISK",
                 "TRADE_TARIFFS", "GEOPOLITICS", "POLICY_REGULATION", "SECTOR_SPECIFIC", "STOCK_SPECIFIC", "NONE"]
# sector -> its % change column in index_levels (what evaluate() checks the sector calls against)
SECTORS = {"BANK": "banknifty_chg", "IT": "nifty_it_chg", "AUTO": "nifty_auto_chg", "FMCG": "nifty_fmcg_chg",
           "METAL": "nifty_metal_chg", "REALTY": "nifty_realty_chg", "PSU_BANK": "nifty_psubank_chg",
           "ENERGY": "nifty_energy_chg", "PHARMA": "nifty_pharma_chg", "MIDCAP": "midcap150_chg",
           "SMALLCAP": "smallcap250_chg"}
SECTOR_NAMES = {"BANK": "Bank Nifty", "IT": "Nifty IT", "AUTO": "Nifty Auto", "FMCG": "Nifty FMCG",
                "METAL": "Nifty Metal", "REALTY": "Nifty Realty", "PSU_BANK": "Nifty PSU Bank",
                "ENERGY": "Nifty Energy", "PHARMA": "Nifty Pharma", "MIDCAP": "Midcap 150", "SMALLCAP": "Smallcap 250"}

DDL = [
    """CREATE TABLE IF NOT EXISTS news_impact (
        article_id INTEGER PRIMARY KEY, assessed_at TIMESTAMP, for_session TEXT, published_at TIMESTAMP,
        headline TEXT, source TEXT, category TEXT, method TEXT, model TEXT, india_relevance REAL,
        nifty_direction TEXT, magnitude TEXT, horizon TEXT, transmission TEXT, sectors_json TEXT, confidence REAL,
        rationale TEXT, actual_chg REAL, hit INTEGER, evaluated_at TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_news_impact_session ON news_impact(for_session)",
    """CREATE TABLE IF NOT EXISTS news_impact_daily (
        for_session TEXT NOT NULL, run_label TEXT NOT NULL, generated_at TIMESTAMP, score REAL, bias TEXT,
        strength TEXT, net_points REAL, n_items INTEGER, n_claude INTEGER, n_rule INTEGER, sectors_json TEXT,
        drivers_json TEXT, brief_json TEXT, brief_method TEXT, fallback_reason TEXT,
        PRIMARY KEY (for_session, run_label))""",
    """CREATE TABLE IF NOT EXISTS news_impact_eval (
        for_session TEXT NOT NULL, run_label TEXT NOT NULL, evaluated_at TIMESTAMP, generated_at TIMESTAMP,
        pre_open INTEGER, predicted_bias TEXT, predicted_score REAL, method TEXT, actual_nifty_chg REAL,
        outcome TEXT, hit INTEGER, sectors_json TEXT, PRIMARY KEY (for_session, run_label))""",
]

SYSTEM = (
    "You are an Indian equity-market strategist. For each numbered news headline (Indian or world), estimate its "
    "likely effect on Indian equities -- the Nifty 50 and the NSE sector indices.\n"
    "Think about how the news reaches India: FII flows, crude oil (India imports ~85% of its oil, so higher crude "
    "is negative for India and for oil marketers, lower crude positive), the rupee, US yields and the dollar, RBI "
    "and government policy, trade and tariffs, geopolitics (Pakistan, China, the Middle East), global risk "
    "appetite, earnings.\n"
    "Be conservative. Most headlines move the Nifty by nothing or SMALL. MEDIUM is for clearly market-wide news "
    "(a Fed or RBI surprise, a sharp crude move, a large FII flow figure, a big index heavyweight result). LARGE "
    "is rare: war or a major attack, an emergency policy move, a crash in a major market.\n"
    "A headline that only REPORTS a market move that already happened (\"Sensex jumps 483 points\", \"market "
    "wrap\", live-blog titles, stocks-to-buy lists) has magnitude NONE and direction NONE. News about a single "
    "company is STOCK_SPECIFIC: Nifty direction NONE unless the company is an index heavyweight and the news is "
    "large; give its sector instead when it plausibly moves the sector.\n"
    "Sectors only from the list given. india_relevance 0..1 = how much the headline matters to Indian equities "
    "at all. confidence 0..1 = how sure you are of the direction. rationale at most 20 words, naming the channel. "
    "No price levels, no point or percentage forecasts, no buy/sell advice."
)
SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "india_relevance": {"type": "number"},
                       "nifty_direction": {"type": "string", "enum": DIRECTIONS},
                       "magnitude": {"type": "string", "enum": MAGNITUDES},
                       "horizon": {"type": "string", "enum": HORIZONS},
                       "transmission": {"type": "string", "enum": TRANSMISSIONS},
                       "sectors": {"type": "array", "items": {"type": "object", "properties": {
                           "sector": {"type": "string", "enum": list(SECTORS)},
                           "direction": {"type": "string", "enum": ["UP", "DOWN"]}},
                           "required": ["sector", "direction"], "additionalProperties": False}},
                       "confidence": {"type": "number"}, "rationale": {"type": "string"}},
        "required": ["i", "india_relevance", "nifty_direction", "magnitude", "horizon", "transmission", "sectors",
                     "confidence", "rationale"],
        "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
BRIEF_SYSTEM = (
    "You write a short pre-trade note for an Indian equities desk: how the news below is likely to affect the "
    "Indian market in the coming session. Use only the drivers, sector reads and market data given; cite driver "
    "numbers. No price levels, no point or percentage forecasts, no buy/sell advice. Say plainly when the news "
    "flow is thin or mixed."
)
BRIEF_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"},
                   "bullets": {"type": "array", "items": {"type": "object", "properties": {
                       "text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "integer"}}},
                       "required": ["text", "refs"], "additionalProperties": False}},
                   "watch": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "bullets", "watch"], "additionalProperties": False}


# ── settings, tables, sessions ─────────────────────────────────────────────
def settings() -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8")).get("news_impact") or {}
    except Exception:
        user = {}
    for k, v in user.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def ensure_tables(conn):
    for ddl in DDL:
        conn.execute(ddl)


def _now(now=None) -> datetime:
    """IST wall-clock, naive -- how news_articles and index_levels store their times."""
    if now is None:
        return datetime.now(IST).replace(tzinfo=None)
    return now.astimezone(IST).replace(tzinfo=None) if now.tzinfo else now


def _is_session(d: date) -> bool:
    try:
        from utils.trading_calendar import is_trading_day
        return bool(is_trading_day(d))
    except Exception:
        return d.weekday() < 5


def target_session(now=None) -> str:
    """The NSE session a call made now is about: today until the 15:30 close on a trading day, else the next."""
    now = _now(now)
    d = now.date()
    if _is_session(d) and now.time() < datetime.strptime("15:30", "%H:%M").time():
        return str(d)
    d += timedelta(days=1)
    while not _is_session(d):
        d += timedelta(days=1)
    return str(d)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return None


# ── which headlines ────────────────────────────────────────────────────────
RECAP = re.compile(r"(sensex|nifty|markets?|stocks?)\b[^|]{0,40}\b(jumps?|falls?|rises?|climbs?|slips?|ends?|"
                   r"closes?|settles?|rebounds?|snaps?|extends?|gains?|drops?|tumbles?|surges?|soars?|sheds?|"
                   r"adds?)\b[^|]{0,25}\b(\d[\d,.]*\s*(points?|pts|%|per ?cent))"
                   r"|market (wrap|highlights|today)|stock market highlights|live updates?|live blog|"
                   r"share market today|stocks? to (buy|watch|sell)|top \d+ stocks|trade setup|gmp today|"
                   r"profit calculator|closing bell|opening bell", re.I)


def is_recap(headline: str) -> bool:
    return bool(RECAP.search(headline or ""))


def pending(conn, now=None, cfg=None) -> list:
    cfg = cfg or settings()
    now = _now(now)
    since = (now - timedelta(hours=float(cfg["window_hours"]))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        dup = "AND a.dup_of IS NULL" if any(r[1] == "dup_of" for r in conn.execute("PRAGMA table_info(news_articles)")) else ""
    except Exception:
        dup = ""
    rows = conn.execute(
        f"SELECT a.id, a.fetched_at, a.headline, a.source, a.category, a.importance, a.sentiment, a.confidence, "
        f"a.classifier FROM news_articles a LEFT JOIN news_impact i ON i.article_id=a.id "
        f"WHERE i.article_id IS NULL AND a.fetched_at>=? AND a.importance IN ('HIGH','MEDIUM') {dup} "
        f"ORDER BY CASE a.importance WHEN 'HIGH' THEN 0 ELSE 1 END, a.fetched_at DESC", (since,)).fetchall()
    out, recaps = [], []
    for r in rows:
        (recaps if is_recap(r["headline"]) else out).append(dict(r))
    return out[:int(cfg["max_items"])], recaps


# ── rule path ──────────────────────────────────────────────────────────────
# What India imports or borrows in: a move in it reads the other way for Indian equities. The move is taken from
# the words next to it ("crude jumps", "oil decline", "elevated oil prices"), not from the whole headline's tone.
_PRICED = r"(crude|brent|wti|oil(?: prices?)?|bond yields?|treasury yields?|us yields?|10-year yields?|dollar(?: index)?|inflation|cpi)"
_UPW = r"(rise|rises|rising|risen|jump|jumps|jumped|surge|surges|surged|soar|soars|climb|climbs|gain|gains|spike|spikes|hits? high|higher|elevated|costlier|firm|firms)"
_DNW = r"(fall|falls|fell|falling|drop|drops|slide|slides|decline|declines|ease|eases|easing|cool|cools|cooling|plunge|plunges|tumble|tumbles|retreat|retreats|slump|slumps|lower|cheaper|softer|slips)"
_UPADJ = r"(elevated|high|higher|costlier|rising|firm|firmer)"
_DNADJ = r"(lower|cheaper|softer|falling|easing)"
PRICED_MOVE = [(re.compile(rf"\b{_PRICED}\b\W+(?:\w+\W+){{0,2}}?{_UPW}\b", re.I), -1),    # "crude jumps"
               (re.compile(rf"\b{_PRICED}\b\W+(?:\w+\W+){{0,2}}?{_DNW}\b", re.I), 1),     # "oil decline"
               (re.compile(rf"\b{_UPADJ}\s+{_PRICED}\b", re.I), -1),                          # "elevated oil prices"
               (re.compile(rf"\b{_DNADJ}\s+{_PRICED}\b", re.I), 1)]                           # "cheaper crude"
# one company's results, orders or share move: not a Nifty call
STOCK_WORDS = re.compile(r"\b(q[1-4]|quarterly results?|net profit|pat|ebitda|order (win|book)|bags|ipo|listing|"
                         r"dividend|buyback|stake)\b|\bstock\b(?! markets?)", re.I)


def priced_move(h: str) -> int:
    """+1 / -1 for India from a crude / yield / dollar / inflation move named in the headline, 0 when none."""
    for rx, sign in PRICED_MOVE:
        if rx.search(h):
            return sign
    return 0


MARKET_WORDS = re.compile(r"\b(rupee|fii|fpi|foreign investors|rbi|repo|inflation|gdp|fed|wall street|global|"
                          r"crude|oil|tariff|budget|gst|bond|yields?|dollar|china|pakistan|war|sanctions?)\b", re.I)
STOCK_CATS = {"EARNINGS", "ORDERS_CONTRACTS", "M_AND_A", "CORPORATE_ACTION", "MANAGEMENT", "RATING_TARGET", "IPO"}
CAT_TRANSMISSION = {"RBI_POLICY": "RATES_LIQUIDITY", "GOVT_POLICY": "POLICY_REGULATION", "MACRO": "RATES_LIQUIDITY",
                    "GLOBAL": "GLOBAL_RISK", "GEOPOLITICS": "GEOPOLITICS", "COMMODITIES": "CRUDE_COMMODITIES",
                    "FLOWS": "FII_FLOWS", "SECTOR": "SECTOR_SPECIFIC", "REGULATORY_LEGAL": "POLICY_REGULATION"}
SECTOR_KEYS = [("PSU_BANK", ("psu bank", "sbi", "bank of baroda", "canara bank", "pnb", "public sector bank")),
               ("BANK", ("bank", "lender", "rbi", "repo", "npa", "credit growth", "deposit")),
               ("IT", ("it stocks", "infosys", "tcs", "wipro", "hcl", "tech mahindra", "software", "nasdaq", "h-1b",
                       "it services")),
               ("AUTO", ("auto", "car sales", "two-wheeler", "vehicle", "maruti", "tata motors", " ev ")),
               ("FMCG", ("fmcg", "consumer staples", "hindustan unilever", "itc ", "nestle", "rural demand")),
               ("METAL", ("metal", "steel", "copper", "aluminium", "aluminum", "iron ore", "zinc")),
               ("REALTY", ("realty", "real estate", "housing", "property")),
               ("ENERGY", ("crude", "oil", "gas", "brent", "opec", "power", "ongc", "reliance", "coal")),
               ("PHARMA", ("pharma", "drug", "usfda", "fda", "healthcare"))]


def assess_rule(a: dict) -> dict:
    h = " " + (a.get("headline") or "").lower() + " "
    cat = a.get("category") or "GENERAL"
    sent = float(a.get("sentiment") or 0)
    pm = priced_move(h)                 # dearer crude / higher yields / a stronger dollar hurt India
    if pm:
        sent = 0.6 * pm
    d = "UP" if sent >= 0.25 else "DOWN" if sent <= -0.25 else "NONE"
    # company news stays off the Nifty; a GENERAL headline is market-wide only when it names a market-wide channel
    stock = not pm and (cat in STOCK_CATS or bool(STOCK_WORDS.search(h)) or
                        (cat == "GENERAL" and not MARKET_WORDS.search(h)))
    mag = "NONE" if d == "NONE" else ("MEDIUM" if a.get("importance") == "HIGH" and not stock else "SMALL")
    sectors = []
    if d != "NONE":
        for s, keys in SECTOR_KEYS:
            if any(k in h for k in keys) and s not in [x["sector"] for x in sectors]:
                sectors.append({"sector": s, "direction": d})
            if len(sectors) == 2:
                break
    return {"india_relevance": 0.2 if stock else 0.6, "nifty_direction": "NONE" if stock else d,
            "magnitude": mag, "horizon": "NEXT_SESSION",
            "transmission": "STOCK_SPECIFIC" if stock else "CRUDE_COMMODITIES" if pm and re.search(r"crude|brent|wti|oil", h)
            else CAT_TRANSMISSION.get(cat, "NONE" if d == "NONE" else "GLOBAL_RISK"),
            "sectors": sectors, "confidence": round(min(0.5, float(a.get("confidence") or 0.3)), 2),
            "rationale": (f"keyword rules: {'dearer' if pm < 0 else 'cheaper'} crude / yields / dollar for India" if pm
                          else f"keyword rules: {cat.lower()} headline, sentiment {sent:+.2f}"),
            "method": "rule", "model": None}


# ── model path ─────────────────────────────────────────────────────────────
def _clip(v, lo=0.0, hi=1.0):
    try:
        return round(max(lo, min(hi, float(v))), 3)
    except (TypeError, ValueError):
        return lo


def _from_model(x: dict, model: str) -> dict:
    secs = []
    for s in x.get("sectors") or []:
        if isinstance(s, dict) and s.get("sector") in SECTORS and s.get("direction") in ("UP", "DOWN") \
                and s["sector"] not in [y["sector"] for y in secs]:
            secs.append({"sector": s["sector"], "direction": s["direction"]})
    d = x.get("nifty_direction") if x.get("nifty_direction") in DIRECTIONS else "NONE"
    m = x.get("magnitude") if x.get("magnitude") in MAGNITUDES else "NONE"
    if d == "NONE":
        m = "NONE" if not secs else m
    return {"india_relevance": _clip(x.get("india_relevance")), "nifty_direction": d, "magnitude": m,
            "horizon": x.get("horizon") if x.get("horizon") in HORIZONS else "NEXT_SESSION",
            "transmission": x.get("transmission") if x.get("transmission") in TRANSMISSIONS else "NONE",
            "sectors": secs, "confidence": _clip(x.get("confidence")),
            "rationale": str(x.get("rationale") or "")[:200], "method": "claude", "model": model}


def assess(arts: list, conn, cfg=None) -> tuple[list, str | None]:
    """(assessments in the order given, why the rule path was used -- None when Claude covered every batch)."""
    cfg = cfg or settings()
    from data import news_ai
    model = news_ai.settings().get("model")
    out, reason = [], None
    bs = max(1, int(cfg["batch_size"]))
    for i in range(0, len(arts), bs):
        batch = arts[i:i + bs]
        got = {}
        if reason is None:
            try:
                user = ("Sectors: " + ", ".join(SECTORS) + "\nHeadlines (source / stored category):\n" +
                        "\n".join(f"{n}. [{a.get('source')}/{a.get('category')}] {a['headline'][:300]}"
                                  for n, a in enumerate(batch)))
                res = news_ai._call(conn, "news_impact", SYSTEM, user, SCHEMA, 300 + 160 * len(batch))
                got = {int(x["i"]): x for x in res.get("items", []) if isinstance(x, dict) and "i" in x}
            except news_ai._Unavailable as e:
                reason = str(e)
                log.info(f"  News impact: Claude unavailable ({reason}) — keyword rules")
            except Exception as e:      # an unparseable batch, a missing SDK: this batch by rules, keep trying
                log.warning(f"  News impact batch by rules ({type(e).__name__}: {e})")
                reason = reason if isinstance(e, (ValueError, KeyError, TypeError)) else f"{type(e).__name__}: {e}"
        for n, a in enumerate(batch):
            out.append(_from_model(got[n], model) if n in got else assess_rule(a))
    return out, reason


def store(conn, arts: list, assessed: list, now=None):
    now = _now(now)
    sess = target_session(now)
    for a, x in zip(arts, assessed):
        conn.execute(
            "INSERT OR REPLACE INTO news_impact (article_id, assessed_at, for_session, published_at, headline, source, "
            "category, method, model, india_relevance, nifty_direction, magnitude, horizon, transmission, "
            "sectors_json, confidence, rationale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["id"], _ts(now), sess, str(a.get("fetched_at"))[:19], a["headline"], a.get("source"), a.get("category"),
             x["method"], x["model"], x["india_relevance"], x["nifty_direction"], x["magnitude"], x["horizon"],
             x["transmission"], json.dumps(x["sectors"]), x["confidence"], x["rationale"]))


def assess_pending(conn, now=None, cfg=None) -> dict:
    cfg = cfg or settings()
    ensure_tables(conn)
    arts, recaps = pending(conn, now, cfg)
    assessed, reason = assess(arts, conn, cfg) if arts else ([], None)
    store(conn, arts, assessed, now)
    # a recap is kept as assessed-NONE so it is not offered again on the next run
    store(conn, recaps, [{"india_relevance": 0.0, "nifty_direction": "NONE", "magnitude": "NONE",
                          "horizon": "INTRADAY", "transmission": "NONE", "sectors": [], "confidence": 0.0,
                          "rationale": "market recap: reports a move, does not cause one", "method": "recap",
                          "model": None} for _ in recaps], now)
    conn.commit()
    return {"assessed": len(arts), "recaps": len(recaps), "claude": sum(x["method"] == "claude" for x in assessed),
            "rule": sum(x["method"] == "rule" for x in assessed), "fallback_reason": reason}


# ── combine ────────────────────────────────────────────────────────────────
def _points(r: dict, direction: str, now: datetime, cfg: dict) -> float:
    sign = 1 if direction == "UP" else -1 if direction == "DOWN" else 0
    mag = float(cfg["magnitude_points"].get(r.get("magnitude") or "NONE", 0))
    if r.get("nifty_direction") == "NONE" and mag == 0 and direction != "NONE":
        mag = 1.0                       # a sector call on an otherwise index-neutral headline
    pub = _dt(r.get("published_at")) or now
    age = max(0.0, (now - pub).total_seconds() / 3600)
    decay = 0.5 ** (age / float(cfg["half_life_hours"]))
    return sign * mag * float(r.get("confidence") or 0) * float(r.get("india_relevance") or 0) * decay


def _score(net: float, cfg: dict) -> tuple[float, str, str]:
    score = round(50 + 50 * math.tanh(net / float(cfg["score_scale"])), 1)
    bias = "POSITIVE" if score >= cfg["positive_at"] else "NEGATIVE" if score <= cfg["negative_at"] else "NEUTRAL"
    dist = abs(score - 50)
    return score, bias, "STRONG" if dist >= 25 else "MODERATE" if dist >= 12 else "WEAK"


def aggregate(conn, now=None, label="manual", cfg=None, brief=None) -> dict:
    cfg = cfg or settings()
    ensure_tables(conn)
    now = _now(now)
    since = (now - timedelta(hours=float(cfg["window_hours"]))).strftime("%Y-%m-%d %H:%M:%S")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM news_impact WHERE published_at>=? AND published_at<=? AND method IN ('claude','rule')",
        (since, now.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()]
    net, drivers, sec = 0.0, [], {}
    for r in rows:
        p = _points(r, r["nifty_direction"], now, cfg)
        net += p
        secs = json.loads(r.get("sectors_json") or "[]")
        for s in secs:
            sp = _points(r, s["direction"], now, cfg)
            e = sec.setdefault(s["sector"], {"net": 0.0, "n": 0})
            e["net"] += sp
            e["n"] += 1
        weight = abs(p) + 0.25 * sum(abs(_points(r, s["direction"], now, cfg)) for s in secs)
        if weight > 0:
            drivers.append({"article_id": r["article_id"], "headline": r["headline"], "source": r["source"],
                            "published_at": str(r["published_at"])[:19], "direction": r["nifty_direction"],
                            "magnitude": r["magnitude"], "transmission": r["transmission"], "sectors": secs,
                            "confidence": r["confidence"], "rationale": r["rationale"], "method": r["method"],
                            "points": round(p, 3), "_w": weight})
    drivers.sort(key=lambda x: -x["_w"])
    drivers = drivers[:8]
    for d in drivers:
        d.pop("_w")
    score, bias, strength = _score(net, cfg)
    sectors = []
    for s, e in sec.items():
        ss, sb, sst = _score(e["net"], cfg)
        sectors.append({"sector": s, "name": SECTOR_NAMES[s], "score": ss, "bias": sb, "strength": sst,
                        "net_points": round(e["net"], 3), "headlines": e["n"]})
    sectors.sort(key=lambda x: -abs(x["score"] - 50))
    n_claude = sum(r["method"] == "claude" for r in rows)
    out = {"for_session": target_session(now), "run_label": label, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "score": score, "bias": bias, "strength": strength, "net_points": round(net, 3), "n_items": len(rows),
           "n_claude": n_claude, "n_rule": len(rows) - n_claude, "sectors": sectors, "drivers": drivers,
           "window_hours": cfg["window_hours"], "informational_only": True}
    b, bm, reason = (brief or make_brief)(conn, out, cfg) if cfg.get("brief") else (None, None, None)
    out.update(brief=b, brief_method=bm, fallback_reason=reason)
    conn.execute(
        "INSERT OR REPLACE INTO news_impact_daily (for_session, run_label, generated_at, score, bias, strength, "
        "net_points, n_items, n_claude, n_rule, sectors_json, drivers_json, brief_json, brief_method, fallback_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (out["for_session"], label, _ts(now), score, bias, strength, out["net_points"], len(rows), n_claude,
         len(rows) - n_claude, json.dumps(sectors), json.dumps(drivers), json.dumps(b) if b else None, bm, reason))
    conn.commit()
    return out


def _market_context(conn) -> str:
    bits = []
    try:
        g = conn.execute("SELECT date, sp500_chg, nasdaq_chg, crude_brent_chg, usd_inr, usd_inr_chg, us_10y "
                         "FROM global_markets ORDER BY date DESC, id DESC LIMIT 1").fetchone()
        if g:
            bits.append(f"global markets {g['date']}: S&P 500 {g['sp500_chg']}%, Nasdaq {g['nasdaq_chg']}%, Brent "
                        f"{g['crude_brent_chg']}%, USD/INR {g['usd_inr']} ({g['usd_inr_chg']}%), US 10Y {g['us_10y']}")
    except Exception:
        pass
    try:
        gift = conn.execute("SELECT date, time, gift_nifty_chg FROM index_levels WHERE gift_nifty_chg IS NOT NULL "
                            "ORDER BY date DESC, time DESC LIMIT 1").fetchone()
        if gift:
            bits.append(f"GIFT Nifty {gift['date']} {gift['time']}: {gift['gift_nifty_chg']}%")
    except Exception:
        pass
    return "; ".join(bits) or "no market data stored"


def make_brief(conn, agg: dict, cfg: dict):
    """(brief, 'claude' | 'rule', why the rule path was used)."""
    if not agg["drivers"]:
        return ({"summary": "No market-moving headlines in the window — no news-driven bias.", "bullets": [],
                 "watch": []}, "rule", None)
    from data import news_ai
    lines = [f"{n}. [{d['direction']}/{d['magnitude']}/{d['transmission']}] {d['headline'][:220]} — {d['rationale']}"
             for n, d in enumerate(agg["drivers"])]
    secs = ", ".join(f"{s['name']} {s['bias']} ({s['score']:.0f})" for s in agg["sectors"][:6]) or "none"
    user = (f"Session {agg['for_session']}. News impact score {agg['score']:.0f}/100 ({agg['bias']}, "
            f"{agg['strength']}) from {agg['n_items']} headlines.\nSectors: {secs}\nMarket data: "
            f"{_market_context(conn)}\nDrivers (direction/magnitude/channel):\n" + "\n".join(lines))
    try:
        b = news_ai._call(conn, "news_impact_brief", BRIEF_SYSTEM, user, BRIEF_SCHEMA, 900)
        for x in b.get("bullets", []):
            x["article_ids"] = [agg["drivers"][r]["article_id"] for r in x.get("refs", [])
                                if isinstance(r, int) and 0 <= r < len(agg["drivers"])]
        return b, "claude", None
    except Exception as e:
        top = agg["drivers"][:3]
        return ({"summary": f"{agg['n_items']} market-relevant headlines; news bias {agg['bias']} "
                            f"({agg['strength'].lower()}), keyword rules, not AI.",
                 "bullets": [{"text": f"{d['direction']} {d['magnitude'].lower()}: {d['headline'][:140]}",
                              "refs": [n], "article_ids": [d["article_id"]]} for n, d in enumerate(top)],
                 "watch": [s["name"] for s in agg["sectors"][:3]]}, "rule", str(e))


# ── tracking ───────────────────────────────────────────────────────────────
def _close_row(conn, session: str):
    cols = ", ".join(["nifty50_chg"] + list(SECTORS.values()))
    return conn.execute(f"SELECT time, {cols} FROM index_levels WHERE date=? AND time>='15:25' AND nifty50_chg "
                        f"IS NOT NULL ORDER BY time DESC LIMIT 1", (session,)).fetchone()


def outcome(chg: float, band: float) -> str:
    return "POSITIVE" if chg > band else "NEGATIVE" if chg < -band else "NEUTRAL"


def evaluate(conn, now=None, cfg=None) -> dict:
    cfg = cfg or settings()
    ensure_tables(conn)
    now = _now(now)
    band = float(cfg["neutral_band_pct"])
    done = skipped = 0
    rows = conn.execute("SELECT d.* FROM news_impact_daily d LEFT JOIN news_impact_eval e ON e.for_session="
                        "d.for_session AND e.run_label=d.run_label WHERE e.for_session IS NULL AND d.for_session<=?",
                        (str(now.date()),)).fetchall()
    for d in rows:
        d = dict(d)
        c = _close_row(conn, d["for_session"])
        gen = _dt(d["generated_at"])
        if not c:
            if (now.date() - date.fromisoformat(d["for_session"])).days > int(cfg["eval_give_up_days"]):
                conn.execute("INSERT OR REPLACE INTO news_impact_eval (for_session, run_label, evaluated_at, "
                             "generated_at, outcome) VALUES (?,?,?,?,'NO_DATA')",
                             (d["for_session"], d["run_label"], _ts(now), d["generated_at"]))
                skipped += 1
            continue
        chg = float(c["nifty50_chg"])
        out = outcome(chg, band)
        secs = []
        for s in json.loads(d.get("sectors_json") or "[]"):
            a = c[SECTORS[s["sector"]]]
            if a is not None and s["bias"] != "NEUTRAL":
                secs.append({"sector": s["sector"], "bias": s["bias"], "actual_chg": a,
                             "hit": int((a > 0) == (s["bias"] == "POSITIVE"))})
        pre = int(bool(gen) and gen < datetime.fromisoformat(d["for_session"] + " 09:15:00"))
        method = "claude" if (d.get("n_claude") or 0) >= (d.get("n_rule") or 0) and d.get("n_claude") else "rule"
        conn.execute("INSERT OR REPLACE INTO news_impact_eval (for_session, run_label, evaluated_at, generated_at, "
                     "pre_open, predicted_bias, predicted_score, method, actual_nifty_chg, outcome, hit, sectors_json) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d["for_session"], d["run_label"], _ts(now), d["generated_at"], pre, d["bias"], d["score"], method,
                      chg, out, int(out == d["bias"]), json.dumps(secs)))
        done += 1
    # each headline's own direction against its session's Nifty close
    items = 0
    for r in conn.execute("SELECT article_id, for_session FROM news_impact WHERE evaluated_at IS NULL AND "
                          "method IN ('claude','rule') AND for_session<=?", (str(now.date()),)).fetchall():
        c = _close_row(conn, r["for_session"])
        if not c:
            continue
        conn.execute("UPDATE news_impact SET actual_chg=?, hit=CASE nifty_direction WHEN 'UP' THEN ?>0 WHEN 'DOWN' "
                     "THEN ?<0 ELSE NULL END, evaluated_at=? WHERE article_id=?",
                     (c["nifty50_chg"], c["nifty50_chg"], c["nifty50_chg"], _ts(now), r["article_id"]))
        items += 1
    conn.commit()
    return {"evaluated": done, "no_data": skipped, "headlines_evaluated": items}


def accuracy(conn, days=90, cfg=None) -> dict:
    """The last pre-open call per session (the one a trader would have read), and two naive baselines."""
    cfg = cfg or settings()
    ensure_tables(conn)
    band = float(cfg["neutral_band_pct"])
    since = str(_now().date() - timedelta(days=int(days)))
    rows = [dict(r) for r in conn.execute(
        "SELECT e.* FROM news_impact_eval e JOIN (SELECT for_session, MAX(generated_at) g FROM news_impact_eval "
        "WHERE pre_open=1 AND hit IS NOT NULL AND for_session>=? GROUP BY for_session) l ON l.for_session="
        "e.for_session AND l.g=e.generated_at WHERE e.pre_open=1 ORDER BY e.for_session", (since,)).fetchall()]
    n = len(rows)

    def rate(rs):
        return None if not rs else round(100.0 * sum(r["hit"] for r in rs) / len(rs), 1)
    outcomes = [r["outcome"] for r in rows]
    common = max(set(outcomes), key=outcomes.count) if outcomes else None
    prev = {}
    for r in conn.execute("SELECT date, nifty50_chg FROM index_levels WHERE time>='15:25' AND nifty50_chg IS NOT "
                          "NULL ORDER BY date").fetchall():
        prev[str(r["date"])[:10]] = r["nifty50_chg"]
    dates = sorted(prev)
    yday_hits = []
    for r in rows:
        earlier = [d for d in dates if d < r["for_session"]]
        if earlier:
            yday_hits.append(int(outcome(prev[earlier[-1]], band) == r["outcome"]))
    secs = [s for r in rows for s in json.loads(r.get("sectors_json") or "[]")]
    item = conn.execute("SELECT method, COUNT(*) n, SUM(hit) h FROM news_impact WHERE hit IS NOT NULL AND "
                        "for_session>=? GROUP BY method", (since,)).fetchall()
    return {"days": int(days), "sessions": n, "hit_rate_pct": rate(rows),
            "by_method": {m: {"sessions": len([r for r in rows if r["method"] == m]),
                              "hit_rate_pct": rate([r for r in rows if r["method"] == m])} for m in ("claude", "rule")},
            "baselines": {} if not n else {
                "always_" + common.lower(): round(100.0 * outcomes.count(common) / n, 1),
                "yesterday_again": None if not yday_hits else round(100.0 * sum(yday_hits) / len(yday_hits), 1)},
            "sector_calls": {"n": len(secs), "hit_rate_pct": None if not secs else
                             round(100.0 * sum(s["hit"] for s in secs) / len(secs), 1)},
            "headline_calls": {r["method"]: {"n": r["n"], "hit_rate_pct": round(100.0 * (r["h"] or 0) / r["n"], 1)}
                               for r in item},
            "neutral_band_pct": band,
            "note": ("too few sessions to judge yet" if n < 20 else
                     "beats both baselines" if rate(rows) is not None and all(
                         b is None or rate(rows) > b for b in (
                             None if not n else 100.0 * outcomes.count(common) / n,
                             None if not yday_hits else 100.0 * sum(yday_hits) / len(yday_hits)))
                     else "does not beat the naive baselines — treat as context only"),
            "recent": [{k: r[k] for k in ("for_session", "run_label", "predicted_bias", "predicted_score",
                                          "actual_nifty_chg", "outcome", "hit", "method")} for r in rows[-15:]]}


# ── read side ──────────────────────────────────────────────────────────────
def latest(conn) -> dict | None:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM news_impact_daily ORDER BY generated_at DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = dict(r)
    d["sectors"] = json.loads(d.pop("sectors_json") or "[]")
    d["drivers"] = json.loads(d.pop("drivers_json") or "[]")
    d["brief"] = json.loads(d.pop("brief_json") or "null")
    d["informational_only"] = True
    return d


def snapshot(conn) -> dict:
    """What /api/news-impact, the pre-market report and the morning brief read."""
    ensure_tables(conn)
    lt = latest(conn)
    recent = [dict(r) for r in conn.execute(
        "SELECT article_id, published_at, headline, source, method, india_relevance, nifty_direction, magnitude, "
        "horizon, transmission, sectors_json, confidence, rationale, actual_chg, hit FROM news_impact WHERE method "
        "IN ('claude','rule') ORDER BY published_at DESC LIMIT 40").fetchall()]
    for r in recent:
        r["sectors"] = json.loads(r.pop("sectors_json") or "[]")
    return {"latest": lt, "recent": recent, "accuracy": accuracy(conn, 90),
            "ai_enabled": _ai_enabled(), "now_ist": _now().strftime("%Y-%m-%d %H:%M")}


def _ai_enabled() -> bool:
    try:
        from data.news_ai import settings as s
        return bool(s()["ai_enabled"])
    except Exception:
        return False


# ── the job ────────────────────────────────────────────────────────────────
def fetch(cfg=None) -> dict:
    """The feeds, through the existing pipeline (de-duplicated, classified, stored once)."""
    cfg = cfg or settings()
    from data.news import run_news_pipeline, GLOBAL_FEEDS
    h = int(cfg["fetch_hours"])
    out = {}
    for name, feeds in (("india", None), ("world", GLOBAL_FEEDS)):
        try:
            r = run_news_pipeline(h, feeds=feeds)
            out[name] = {"status": r.get("status"), "stored": r.get("rows"), "fetched": r.get("fetched")}
        except Exception as e:
            out[name] = {"status": "FAILED", "error": str(e)}
    return out


def run(label: str = "manual", now=None, fetch_feeds=None) -> dict:
    cfg = settings()
    if not cfg.get("enabled", True):
        return {"status": "SKIPPED", "rows": 0, "reason": "news_impact.enabled is false"}
    feeds = fetch(cfg) if (cfg["fetch_feeds"] if fetch_feeds is None else fetch_feeds) else None
    from db.schema import get_connection
    conn = get_connection()
    try:
        a = assess_pending(conn, now, cfg)
        g = aggregate(conn, now, label, cfg)
        e = evaluate(conn, now, cfg)
    finally:
        conn.close()
    log.info(f"  📰➜📈 News impact {g['for_session']} ({label}): {g['score']:.0f} {g['bias']} {g['strength']} from "
             f"{g['n_items']} headline(s); {a['assessed']} new ({a['claude']} Claude, {a['rule']} rules), "
             f"{e['evaluated']} call(s) scored")
    return {"status": "SUCCESS", "rows": a["assessed"], "for_session": g["for_session"], "score": g["score"],
            "bias": g["bias"], "strength": g["strength"], "assessed": a, "evaluated": e, "feeds": feeds,
            "brief_method": g["brief_method"], "fallback_reason": a["fallback_reason"] or g["fallback_reason"]}


def run_evaluate() -> dict:
    from db.schema import get_connection
    conn = get_connection()
    try:
        r = evaluate(conn)
    finally:
        conn.close()
    return {"status": "SUCCESS", "rows": r["evaluated"], **r}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP news impact estimate")
    ap.add_argument("--label", default="manual")
    ap.add_argument("--no-fetch", action="store_true", help="use the headlines already stored")
    ap.add_argument("--accuracy", action="store_true", help="print the tracking record and exit")
    args = ap.parse_args()
    if args.accuracy:
        from db.schema import get_connection
        c = get_connection()
        try:
            print(json.dumps(accuracy(c), indent=2, default=str))
        finally:
            c.close()
    else:
        print(json.dumps(run(args.label, fetch_feeds=False if args.no_fetch else None), indent=2, default=str))
