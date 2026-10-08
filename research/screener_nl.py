"""
W39 Phase 4 (item 1, NL-01..03) — ask the screener in English: "debt-free capital goods companies with ROCE
above 20 % near their 52-week high" becomes `debt_equity < 0.1 AND roce_pct > 20 AND from_52w_high_pct > -5
AND industry IN ("Capital Goods")`. The text is only ever turned into ATIP's own query language, which the
screener's safe parser validates (no eval, no SQL); nothing runs until the user has seen, and can edit, the
query.

Two paths, the same output: {query, sort, desc, explanation, unmatched, mode, fallback_reason}

    claude   config.json "screener_ai": {"enabled": false, "model": "claude-sonnet-5-5",
             "daily_cost_cap_usd": 0.5}. One request with the field catalogue, the presets and the grammar as a
             cached system prompt and a JSON-schema structured output. A query the parser rejects is sent back
             once with the parser's error. Tokens and cost go to ai_usage_log (purpose "screener_nl") and count
             against this feature's own daily cap.
    rules    always available, and used whenever the Claude path is off, over its cap, refused (the key in .env
             returned 401, KNOWN_ISSUES KD-001) or fails: preset names; "<field> above / below / at least /
             between ... <number>"; phrases such as debt-free, no pledge, near the 52-week high, above the
             200-DMA; technical scan names (golden cross, Supertrend buy ...); industries; "sorted by" / "top N".
             What it could not map is returned as `unmatched`, so the page can say so.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from research import screener as SC

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "model": "claude-sonnet-5-5", "daily_cost_cap_usd": 0.5}
PURPOSE = "screener_nl"
MAX_TEXT = 500


def settings() -> dict:
    try:
        from ops.config import load
        raw = load().get("screener_ai") or {}
    except Exception:
        raw = {}
    return {k: type(v)(raw[k]) if k in raw else v for k, v in DEFAULTS.items()}


class Unavailable(RuntimeError):
    pass


# ── the rule path ────────────────────────────────────────────────────────────

_NUM = r"(-?\d+(?:\.\d+)?)\s*(%|percent|crores|crore|cr\b|x\b)?"
_OPS = [
    (r"between\s+" + _NUM + r"\s+(?:and|to|-)\s+" + _NUM, "between"),
    (r"(?:>=|at least|minimum of|min(?:imum)?|not less than|no less than)\s*" + _NUM, ">="),
    (r"(?:<=|at most|maximum of|max(?:imum)?|not more than|no more than|up to)\s*" + _NUM, "<="),
    (r"(?:>|above|over|greater than|more than|higher than|exceeding|exceeds)\s*" + _NUM, ">"),
    (r"(?:<|below|under|less than|lower than|beneath)\s*" + _NUM, "<"),
    (r"(?:=|equal to|equals)\s*" + _NUM, "="),
]

PHRASES = [                                  # (pattern, condition, what it means)
    (r"\bhigh roce\b", "roce_pct > 20", "ROCE over 20 %"),
    (r"\bhigh roe\b", "roe_pct > 18", "ROE over 18 %"),
    (r"\blow (?:p/?e|valuations?)\b", "pe < 15 AND pe > 0", "P/E under 15"),
    (r"\bhigh dividends?(?: yield)?\b", "dividend_yield_pct > 3", "dividend yield over 3 %"),
    (r"\bhigh (?:sales |revenue )?growth\b", "revenue_growth_pct > 20", "revenue growth over 20 %"),
    (r"\b(debt[- ]free|zero debt|no debt|without debt)\b", "debt_equity < 0.1", "debt-free"),
    (r"\b(low debt|little debt)\b", "debt_equity < 0.5", "low debt"),
    (r"\b(no pledg\w*|zero pledg\w*|unpledged)\b", "pledged_pct < 1", "no promoter pledge"),
    (r"\bnear (?:the |its |their )?52[- ]?week high\b", "from_52w_high_pct > -5", "within 5 % of the 52-week high"),
    (r"\bnear (?:the |its |their )?52[- ]?week low\b", "from_52w_low_pct < 5", "within 5 % of the 52-week low"),
    (r"\babove (?:the |its |their )?200[- ]?(?:dma|day|sma|day moving average)\b", "above_200dma = 1", "above the 200-DMA"),
    (r"\bbelow (?:the |its |their )?200[- ]?(?:dma|day|sma|day moving average)\b", "above_200dma = 0", "below the 200-DMA"),
    (r"\b(profitable|making profits?)\b", "eps_ttm > 0", "profitable"),
    (r"\b(loss[- ]making)\b", "eps_ttm < 0", "loss-making"),
    (r"\b(dividend payers?|pays? (?:a )?dividends?)\b", "dividend_yield_pct > 0", "pays a dividend"),
    (r"\b(undervalued|buy[- ]rated|rated buy)\b", 'research_rating IN ("BUY", "ADD")', "research rating BUY / ADD"),
    (r"\b(overvalued|sell[- ]rated|rated sell)\b", 'research_rating IN ("SELL", "REDUCE")', "research rating SELL / REDUCE"),
    (r"\b(promoters? (?:are )?(?:buying|adding|increasing))\b", "promoter_change_pts > 0", "promoter stake up"),
    (r"\b(strong buy|technically strong)\b", 'tech_rating_label = "STRONG_BUY"', "technical rating STRONG BUY"),
    (r"\b(uptrend|up-trend|bullish trend)\b", 'mtf_alignment = "BULL"', "daily and weekly ratings bullish"),
    (r"\b(oversold)\b", "rsi_14 < 30", "RSI under 30"),
    (r"\b(overbought)\b", "rsi_14 > 70", "RSI over 70"),
    (r"\b(large[- ]?caps?)\b", 'cap_bucket = "LARGE"', "large cap"),
    (r"\b(mid[- ]?caps?)\b", 'cap_bucket = "MID"', "mid cap"),
    (r"\b(small[- ]?caps?)\b", 'cap_bucket = "SMALL"', "small cap"),
]

# phrases that name a field better than its key does (longest first wins)
FIELD_WORDS = {
    "roce": "roce_pct", "return on capital": "roce_pct", "roe": "roe_pct", "return on equity": "roe_pct",
    "roa": "roa_pct", "p/e": "pe", "pe ratio": "pe", "pe": "pe", "price to earnings": "pe", "p/b": "pb", "pb": "pb",
    "price to book": "pb", "peg": "peg", "dividend yield": "dividend_yield_pct", "yield": "dividend_yield_pct",
    "debt to equity": "debt_equity", "debt/equity": "debt_equity", "d/e": "debt_equity", "market cap": "market_cap_cr",
    "market capitalisation": "market_cap_cr", "market capitalization": "market_cap_cr", "mcap": "market_cap_cr",
    "sales growth": "revenue_growth_pct", "revenue growth": "revenue_growth_pct", "profit growth": "profit_growth_pct",
    "eps growth": "eps_growth_pct", "earnings growth": "eps_growth_pct", "net margin": "net_margin_pct",
    "operating margin": "operating_margin_pct", "promoter holding": "promoter_pct", "promoter stake": "promoter_pct",
    "pledge": "pledged_pct", "rsi": "rsi_14", "adx": "adx_14", "rs rating": "rs_rating", "relative strength": "rs_rating",
    "1 year return": "return_1y_pct", "one year return": "return_1y_pct", "1-year return": "return_1y_pct",
    "price": "price", "current ratio": "current_ratio", "interest cover": "interest_coverage",
    "interest coverage": "interest_coverage", "free cash flow yield": "fcf_yield_pct", "fcf yield": "fcf_yield_pct",
    "upside": "research_upside_pct", "scorecard": "checks_passed", "checks passed": "checks_passed",
    "atip score": "atip_score", "delivery": "delivery_pct", "volume ratio": "vol_ratio",
}


FILLER = {"stocks", "stock", "companies", "company", "with", "and", "that", "are", "the", "a", "an", "of", "in", "is",
          "show", "me", "find", "list", "which", "have", "has", "their", "its", "all", "where", "whose", "good", "%",
          "percent", "shares", "for", "to", "give", "get", "on", "nse", "india", "indian", "sector", "industry",
          "please", "top", "best", "only", "also", "or", "but", "than", "be", "today", "now", "first", "names",
          "preset", "screen", "run", "use", "those", "these", "some", "any"}


def _field_phrases() -> list:
    out = dict(FIELD_WORDS)
    for k, m in SC.FIELDS.items():
        if m["kind"] == "num" and not k.startswith("scan_"):
            out.setdefault(m["label"].lower(), k)
            for a in m["aliases"]:
                out.setdefault(a.replace("_", " ").lower(), k)
            out.setdefault(k.replace("_", " "), k)
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def _scan_phrases() -> list:
    out = []
    for k, m in SC.FIELDS.items():
        if k.startswith("scan_"):
            name = re.sub(r"\s*\(.*?\)", "", m["label"]).lower().strip()
            out.append((name, k))
    return sorted(out, key=lambda kv: -len(kv[0]))


def _num(v, unit):
    x = float(v)
    return int(x) if x == int(x) else x


def translate_rules(text: str, industries=()) -> dict:
    """Deterministic English -> query. Returns the same shape as translate()."""
    t = re.sub(r"(\d),(?=\d{2,3}\b)", r"\1", str(text or "").lower())       # 1,000 / 1,00,000 -> plain digits
    t = " " + re.sub(r"\s+", " ", t).strip() + " "
    used, conds, notes = [], [], []

    def take(span):
        used.append(span)

    core = " ".join(w for w in re.findall(r"[a-z0-9/-]+", t) if w not in FILLER)
    for p in SC.PRESETS:                                  # a request that IS a preset's name gets the preset
        if core == " ".join(w for w in re.findall(r"[a-z0-9/-]+", p["name"].lower()) if w not in FILLER):
            return {"query": p["query"], "sort": p.get("sort"), "desc": p.get("desc", True),
                    "explanation": f"the preset “{p['name']}”: {p['description']}", "unmatched": []}
    for pat, cond, what in PHRASES:
        m = re.search(pat, t)
        if m:
            conds.append(cond)
            notes.append(what)
            take(m.span())
    for name, key in _scan_phrases():
        i = t.find(" " + name + " ")
        if i >= 0 and not any(a <= i < b for a, b in used):
            conds.append(f"{key} = 1")
            notes.append(f"{SC.FIELDS[key]['label']} today")
            take((i, i + len(name) + 2))
    ind = [x for x in industries if x and f" {x.lower()} " in t]
    if ind:
        conds.append("industry IN (" + ", ".join(f'"{x}"' for x in sorted(set(ind))) + ")")
        notes.append("industry " + " or ".join(sorted(set(ind))))
        for x in ind:
            i = t.find(f" {x.lower()} ")
            take((i, i + len(x) + 2))
    for phrase, key in _field_phrases():
        for m in re.finditer(r"(?<![a-z0-9_])" + re.escape(phrase) + r"(?![a-z0-9_])", t):
            if any(a <= m.start() < b for a, b in used):
                continue
            rest = t[m.end():m.end() + 60]
            for op_pat, op in _OPS:
                om = re.match(r"\s*(?:is\s+|of\s+|should be\s+)?" + op_pat, rest)
                if not om:
                    continue
                if op == "between":
                    lo, hi = _num(om.group(1), om.group(2)), _num(om.group(3), om.group(4))
                    conds += [f"{key} >= {lo}", f"{key} <= {hi}"]
                    notes.append(f"{SC.FIELDS[key]['label']} between {lo} and {hi}")
                else:
                    v = _num(om.group(1), om.group(2))
                    conds.append(f"{key} {op} {v}")
                    notes.append(f"{SC.FIELDS[key]['label']} {op} {v}")
                take((m.start(), m.end() + om.end()))
                break
    sort, desc = None, True
    sm = re.search(r"\b(?:sorted|sort|ranked|rank|order(?:ed)?) by (?:the )?(highest |lowest )?([a-z/ -]+)", t)
    if sm:
        tail = sm.group(2).strip()
        for phrase, key in _field_phrases():
            if tail == phrase or tail.startswith(phrase + " "):
                sort, desc = key, sm.group(1) != "lowest "
                take((sm.start(), sm.start(2) + len(phrase)))
                break
    cm = re.search(r"\b(cheapest|lowest p/?e)(?: first)?\b", t)
    if sort is None and cm:
        sort, desc = "pe", False
        take(cm.span())
        if "pe > 0" not in " AND ".join(conds):
            conds.append("pe > 0")
    words = re.findall(r"[a-z0-9/%.-]+", "".join(" " if any(a <= i < b for a, b in used) else ch
                                                 for i, ch in enumerate(t)))
    unmatched = [w for w in words if w not in FILLER and not re.fullmatch(r"-?\d+(\.\d+)?", w)]
    seen, uniq = set(), []
    for c in conds:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return {"query": " AND ".join(uniq), "sort": sort, "desc": desc,
            "explanation": "; ".join(notes) if notes else "", "unmatched": unmatched}


# ── the Claude path ──────────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "sort": {"type": ["string", "null"]},
        "desc": {"type": "boolean"},
        "explanation": {"type": "string"},
        "unmatched": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query", "sort", "desc", "explanation", "unmatched"],
    "additionalProperties": False,
}


def system_prompt(industries=()) -> str:
    fields = []
    for k, m in SC.FIELDS.items():
        if k.startswith("scan_"):
            continue
        unit = f" [{m['unit']}]" if m["unit"] else ""
        kind = " (text)" if m["kind"] == "text" else ""
        al = f"; also: {', '.join(m['aliases'])}" if m["aliases"] else ""
        desc = f" -- {m['description']}" if m["description"] else ""
        fields.append(f"{k}{kind}{unit}: {m['label']}{al}{desc}")
    scans = [f"{k}: {m['label']}" for k, m in SC.FIELDS.items() if k.startswith("scan_")]
    presets = [f"{p['name']}: {p['query']}" for p in SC.PRESETS]
    return (
        "You translate an Indian equity investor's request into ONE query in ATIP's stock-screener language. "
        "Return only what the JSON schema asks for.\n\n"
        "Grammar: conditions `field op value` with op one of > >= < <= = != ; `field IN (\"A\", \"B\")`; "
        "`field CONTAINS \"text\"` for text fields; combine with AND, OR, NOT and parentheses (AND binds tighter "
        "than OR). Text values in double quotes. Use only the field keys listed below. Percentages are in percent "
        "(roce_pct > 20 means 20 %); money fields ending in _cr are in rupees crore; ratios are plain numbers. "
        "Scan fields (scan_*) are 1 when the scan fired on the latest session, else 0.\n"
        "If part of the request cannot be expressed with these fields, leave it out and list the words in "
        "`unmatched`; never invent a field. `sort` is a field key or null; `desc` true for highest first. "
        "`explanation` says in one short sentence what the query selects.\n\n"
        "FIELDS:\n" + "\n".join(fields) + "\n\nSCANS:\n" + "\n".join(scans) + "\n\nPRESETS (examples):\n" +
        "\n".join(presets) + ("\n\nINDUSTRIES (exact spelling for industry IN (...)):\n" + ", ".join(industries)
                              if industries else "")
    )


def _spent_today(conn) -> float:
    try:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage_log WHERE day=? AND purpose=?",
                         (str(date.today()), PURPOSE)).fetchone()
        return float(r[0] or 0)
    except Exception:
        return 0.0


def _client():
    from data.news_ai import _client as news_client
    return news_client()


def _ask(conn, client, model, system, messages) -> dict:
    from data.news_ai import _log_usage
    try:
        resp = client.messages.create(
            model=model, max_tokens=800,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages, output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    except Exception as e:
        _log_usage(conn, PURPOSE, model, None, ok=False, error=e)
        if getattr(e, "status_code", None) == 401 or type(e).__name__ == "AuthenticationError":
            raise Unavailable("Anthropic API key rejected (401)") from e
        raise Unavailable(f"Anthropic API error: {type(e).__name__}: {str(e)[:160]}") from e
    _log_usage(conn, PURPOSE, model, resp.usage)
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise Unavailable(f"stop_reason {resp.stop_reason}")
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    return json.loads(text)


def translate_claude(conn, text: str, industries=(), client=None) -> dict:
    s = settings()
    if not s["enabled"]:
        raise Unavailable("screener_ai.enabled is false")
    if _spent_today(conn) >= float(s["daily_cost_cap_usd"]):
        raise Unavailable(f"daily cost cap {s['daily_cost_cap_usd']} USD reached")
    client = client or _client()
    system = system_prompt(industries)
    messages = [{"role": "user", "content": text}]
    out = _ask(conn, client, s["model"], system, messages)
    try:
        SC.parse(out["query"])
    except SC.ScreenError as e:
        messages += [{"role": "assistant", "content": json.dumps(out)},
                     {"role": "user", "content": f"The screener rejected that query: {e}. Return a corrected one."}]
        out = _ask(conn, client, s["model"], system, messages)
        SC.parse(out["query"])                           # a second failure falls back to the rules
    if out.get("sort") and out["sort"] not in SC.FIELDS:
        out["sort"] = None
    return out


# ── both ─────────────────────────────────────────────────────────────────────

def translate(conn, text: str, industries=None, client=None) -> dict:
    """English -> {query, sort, desc, explanation, unmatched, mode, fallback_reason, valid, error}. Never runs it."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    if industries is None:
        try:
            rows, _ = SC.snapshot(conn)
            industries = sorted({r["industry"] for r in rows if r.get("industry")})
        except Exception:
            industries = []
    reason = None
    try:
        out = translate_claude(conn, text, industries, client)
        out["mode"] = "claude"
    except Exception as e:                                # any failure: the rules answer instead
        reason = str(e) if isinstance(e, Unavailable) else f"{type(e).__name__}: {str(e)[:160]}"
        out = translate_rules(text, industries)
        out["mode"] = "rules"
    out["fallback_reason"] = reason
    out["text"] = text
    try:
        if not out["query"]:
            raise SC.ScreenError("nothing in the text could be mapped to a screener field")
        SC.parse(out["query"])
        out["valid"], out["error"] = True, None
    except SC.ScreenError as e:
        out["valid"], out["error"] = False, str(e)
    return out
