"""
AI chat assistant (W36: ML-16).

Ask ATIP questions in plain language -- "why is TATAMOTORS a BUY?", "what is the market doing?",
"which stocks have the highest crash risk?", "how is my portfolio?". Claude answers by calling
READ-ONLY tools over ATIP's own tables; it has no tool that can change anything, place, modify or
cancel an order, or alter a strategy, model or setting.

    ask(conn, question, conversation_id=None) -> {conversation_id, answer, tools_used, mode, ...}

Modes
    claude          assistant.enabled and an Anthropic credential: a tool-use loop (at most
                    max_tool_rounds rounds) with the configured model and effort. Every request's
                    tokens and cost go to ai_usage_log (purpose 'assistant', W28 ledger); the assistant
                    has its OWN daily cap (assistant.daily_cost_cap_usd), separate from news AI's.
                    Server-side refusal fallback is on (fallbacks "default").
    deterministic   otherwise (disabled, no key, cap reached, API error): the W5 rule-based
                    ml.assistant.answer() over the same data, labelled as such.

Conversations: assistant_conversation / assistant_message keep the question, the answer, the tools
called and the mode. Earlier turns are sent back as plain text (question / answer), not tool blocks,
so a long conversation stays cheap and replays nothing stale.

Config (config.json "assistant", off by default):
    {"enabled": false, "model": "claude-opus-5-5", "effort": "medium", "daily_cost_cap_usd": 2.0,
     "max_tool_rounds": 6, "history_turns": 6}
Answers describe ATIP's data and methods; they are not personalised investment advice.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "model": "claude-opus-5-5", "effort": "medium", "daily_cost_cap_usd": 2.0,
            "max_tool_rounds": 6, "history_turns": 6}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_CHARS = 12000

SYSTEM = (
    "You are ATIP's research assistant for an Indian equity trader. ATIP is the user's own analytics platform: "
    "it scores NSE stocks daily (ATIP score, VPI, RRI, MRI, CRI crash risk, ZPI buy zone, MSI market sentiment), "
    "tracks market health, news, strategies and a paper/live portfolio.\n"
    "Answer ONLY from what the tools return. Call tools to get facts; never guess numbers, dates or signals. "
    "Say which date the data is from. If a tool returns nothing, say ATIP has no data for it.\n"
    "Explain scores and signals in plain language: what drives them (the components), not just the number. "
    "You cannot place, modify or cancel orders or change any setting, and you must not offer to. "
    "Do not give personalised buy/sell instructions or position sizes; describe what ATIP's data and signals say "
    "and the risks, and leave the decision to the user. Be concise: short paragraphs or a compact list."
)

TOOLS = [
    {"name": "market_overview", "description": "Latest market health (regime, score, breadth), NSE index levels and "
     "changes, India VIX, FII/DII flows, global markets and the latest market news brief.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "stock_detail", "description": "Everything ATIP knows about one NSE stock: latest ATIP scores and "
     "components, top factors, signal, price, ML predictions, strategy decisions and recent news.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string", "description": "NSE symbol, e.g. TCS"}},
                      "required": ["symbol"], "additionalProperties": False}},
    {"name": "find_symbol", "description": "Find NSE symbols whose company name or symbol contains the text.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                      "additionalProperties": False}},
    {"name": "ranked_list", "description": "Ranked stock lists from the latest scored session.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["top_atip", "buy_signals", "sell_signals", "crash_risk", "buy_zone",
                                             "momentum"]},
         "n": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["kind"], "additionalProperties": False}},
    {"name": "portfolio", "description": "The user's latest synced holdings with quantity, average price, current "
     "price, P&L % and each holding's ATIP score / CRI / signal.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "news", "description": "Recent classified news headlines (optionally for one symbol) with sentiment.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                       "hours": {"type": "integer", "minimum": 1, "maximum": 168}},
                      "additionalProperties": False}},
    {"name": "score_history", "description": "A stock's ATIP score, CRI and signal over the last N sessions.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"},
                                                       "sessions": {"type": "integer", "minimum": 5, "maximum": 120}},
                      "required": ["symbol"], "additionalProperties": False}},
]


def settings() -> dict:
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        raw = cfg.get("assistant") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["enabled"] = out.get("enabled") is True
    return out


# ── tools (read-only) ─────────────────────────────────────────────────────
def _rows(conn, sql, *a):
    try:
        return [dict(r) for r in conn.execute(sql, a).fetchall()]
    except Exception as e:
        return [{"error": str(e)[:200]}]


def _latest_session(conn):
    r = conn.execute("SELECT MAX(date) FROM ai_scores").fetchone()
    return str(r[0])[:10] if r and r[0] else None


def t_market_overview(conn, _):
    out = {"market_health": _rows(conn, "SELECT date, mh_score, regime, breadth, pct_advancing, vix_level FROM "
                                        "market_health ORDER BY date DESC LIMIT 1"),
           "indexes": _rows(conn, "SELECT * FROM index_levels ORDER BY date DESC, time DESC LIMIT 1"),
           "fii_dii": _rows(conn, "SELECT * FROM fii_dii_market ORDER BY date DESC LIMIT 3"),
           "global": _rows(conn, "SELECT date, time, sp500_chg, nasdaq_chg, nikkei_chg, hangseng_chg, crude_brent, "
                                 "gold, usd_inr, us_10y, global_sentiment FROM global_markets ORDER BY date DESC, "
                                 "created_at DESC LIMIT 1")}
    try:
        from data.news_ai import latest_summary
        out["news_brief"] = latest_summary(conn)
    except Exception:
        out["news_brief"] = None
    return out


def t_stock_detail(conn, a):
    sym = str(a.get("symbol") or "").upper().strip()
    if not sym:
        return {"error": "symbol is required"}
    from ml.assistant import explain_symbol
    out = explain_symbol(conn, sym)
    out["price"] = _rows(conn, "SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol=? "
                               "ORDER BY date DESC LIMIT 1", sym)
    out["news"] = _rows(conn, "SELECT fetched_at, headline, sentiment, category FROM news_articles WHERE "
                              "symbols_mentioned LIKE ? ORDER BY fetched_at DESC LIMIT 8", f'%"{sym}"%')
    return out


def t_find_symbol(conn, a):
    text = str(a.get("text") or "").strip().upper()
    if len(text) < 2:
        return {"error": "give at least 2 characters"}
    try:
        from data.companies import load_company_names
        names = load_company_names() or {}
    except Exception:
        names = {}
    hits = [{"symbol": s, "name": n} for s, n in names.items() if text in s or text in str(n).upper()]
    return {"matches": hits[:20]}


LIST_SQL = {
    "top_atip": "atip_score DESC", "buy_signals": "atip_score DESC", "sell_signals": "cri DESC",
    "crash_risk": "cri DESC", "buy_zone": "zpi DESC", "momentum": "mri DESC",
}


def t_ranked_list(conn, a):
    kind = a.get("kind")
    if kind not in LIST_SQL:
        return {"error": f"kind must be one of {sorted(LIST_SQL)}"}
    n = max(1, min(int(a.get("n") or 15), 50))
    d = _latest_session(conn)
    where = {"buy_signals": " AND signal='BUY'", "sell_signals": " AND signal='SELL'"}.get(kind, "")
    return {"session": d, "kind": kind, "rows": _rows(
        conn, f"SELECT symbol, atip_score, vpi, rri, mri, cri, zpi, signal, top_factor_1, top_factor_2 FROM ai_scores "
              f"WHERE date=?{where} ORDER BY {LIST_SQL[kind]} LIMIT ?", d, n)}


def t_portfolio(conn, _):
    d = conn.execute("SELECT MAX(date) FROM portfolio_holdings").fetchone()[0]
    s = _latest_session(conn)
    return {"as_of": str(d)[:10] if d else None, "holdings": _rows(
        conn, "SELECT h.symbol, h.qty, h.avg_price, h.cmp, h.pnl_pct, h.weight_pct, s.atip_score, s.cri, s.signal "
              "FROM portfolio_holdings h LEFT JOIN ai_scores s ON s.symbol=h.symbol AND s.date=? WHERE h.date=? "
              "ORDER BY h.weight_pct DESC", s, d)} if d else {"as_of": None, "holdings": []}


def t_news(conn, a):
    hours = max(1, min(int(a.get("hours") or 24), 168))
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    sym = str(a.get("symbol") or "").upper().strip()
    if sym:
        return {"symbol": sym, "rows": _rows(conn, "SELECT fetched_at, headline, source, sentiment, importance, category "
                                                   "FROM news_articles WHERE fetched_at>=? AND symbols_mentioned LIKE ? "
                                                   "ORDER BY fetched_at DESC LIMIT 25", since, f'%"{sym}"%')}
    return {"rows": _rows(conn, "SELECT fetched_at, headline, source, sentiment, importance, category FROM news_articles "
                                "WHERE fetched_at>=? ORDER BY importance='HIGH' DESC, fetched_at DESC LIMIT 25", since)}


def t_score_history(conn, a):
    sym = str(a.get("symbol") or "").upper().strip()
    n = max(5, min(int(a.get("sessions") or 30), 120))
    rows = _rows(conn, "SELECT date, atip_score, cri, zpi, signal FROM ai_scores WHERE symbol=? ORDER BY date DESC "
                       "LIMIT ?", sym, n)
    return {"symbol": sym, "rows": list(reversed(rows))}


HANDLERS = {"market_overview": t_market_overview, "stock_detail": t_stock_detail, "find_symbol": t_find_symbol,
            "ranked_list": t_ranked_list, "portfolio": t_portfolio, "news": t_news, "score_history": t_score_history}


def run_tool(conn, name, args) -> str:
    fn = HANDLERS.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        out = json.dumps(fn(conn, args or {}), default=str)
    except Exception as e:
        out = json.dumps({"error": f"{type(e).__name__}: {e}"[:300]})
    return out if len(out) <= MAX_TOOL_CHARS else out[:MAX_TOOL_CHARS] + '..."(truncated)"'


# ── conversation store ────────────────────────────────────────────────────
def _history(conn, cid, turns):
    rows = conn.execute("SELECT question, answer FROM assistant_message WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                        (cid, int(turns))).fetchall()
    msgs = []
    for q, a in reversed(rows):
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a or "(no answer)"}]
    return msgs


def _store(conn, cid, question, answer, mode, model, tools, cost):
    now = datetime.now()
    if not conn.execute("SELECT 1 FROM assistant_conversation WHERE conversation_id=?", (cid,)).fetchone():
        conn.execute("INSERT INTO assistant_conversation (conversation_id,title,created_at,updated_at) VALUES (?,?,?,?)",
                     (cid, question[:80], now, now))
    conn.execute("UPDATE assistant_conversation SET updated_at=? WHERE conversation_id=?", (now, cid))
    conn.execute("INSERT INTO assistant_message (conversation_id,asked_at,question,answer,mode,model,tools_json,cost_usd) "
                 "VALUES (?,?,?,?,?,?,?,?)", (cid, now, question, answer, mode, model, json.dumps(tools), cost))
    conn.commit()


def _spent_today(conn) -> float:
    try:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage_log WHERE day=? AND purpose='assistant'",
                         (str(date.today()),)).fetchone()
        return float(r[0] or 0)
    except Exception:
        return 0.0


def _deterministic(conn, question):
    from ml.assistant import answer
    import re
    sym = next((w for w in re.findall(r"\b[A-Z][A-Z0-9&-]{1,19}\b", question) if
                conn.execute("SELECT 1 FROM ai_scores WHERE symbol=? LIMIT 1", (w,)).fetchone()), None)
    if not sym:
        mh = t_market_overview(conn, {}).get("market_health") or [{}]
        m = mh[0] if mh else {}
        return ("(Rule-based answer: the AI assistant is off.) Name an NSE symbol in capitals, e.g. 'Why is TCS a BUY?', "
                f"for a stock explanation. Latest market health: {m.get('regime', 'n/a')} "
                f"(score {m.get('mh_score', 'n/a')}, {m.get('date', 'no date')}).")
    r = answer(conn, question, sym)
    text = r.get("answer") if isinstance(r, dict) else None
    if isinstance(text, list):
        text = "\n".join(map(str, text))
    return "(Rule-based answer: the AI assistant is off.)\n" + (text or json.dumps(r, default=str, indent=1)[:4000])


def ask(conn, question: str, conversation_id: str | None = None) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > 2000:
        raise ValueError("question is too long (2,000 characters max)")
    cid = conversation_id or ("CV" + uuid.uuid4().hex[:14].upper())
    s = settings()
    reason = None
    if not s["enabled"]:
        reason = "assistant.enabled is false"
    elif _spent_today(conn) >= float(s["daily_cost_cap_usd"]):
        reason = f"daily assistant cost cap ${s['daily_cost_cap_usd']} reached"
    if reason is None:
        try:
            return _claude(conn, question, cid, s)
        except Exception as e:                       # any API / SDK failure -> labelled fallback
            reason = f"Claude unavailable ({type(e).__name__}: {str(e)[:160]})"
            log.warning(f"  assistant: {reason}")
    ans = _deterministic(conn, question)
    _store(conn, cid, question, ans, "deterministic", None, [], 0.0)
    return {"conversation_id": cid, "answer": ans, "mode": "deterministic", "fallback_reason": reason, "tools_used": []}


def _claude(conn, question, cid, s) -> dict:
    import anthropic
    from data.news_ai import _log_usage, cost_usd
    client = anthropic.Anthropic(max_retries=2, timeout=120.0)
    messages = _history(conn, cid, s["history_turns"]) + [{"role": "user", "content": question}]
    used, cost, resp = [], 0.0, None
    for _ in range(int(s["max_tool_rounds"]) + 1):
        try:
            resp = client.beta.messages.create(
                model=s["model"], max_tokens=4000, betas=[FALLBACK_BETA], fallbacks="default",
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS, output_config={"effort": s["effort"]}, messages=messages)
        except anthropic.APIError as e:
            _log_usage(conn, "assistant", s["model"], None, ok=False, error=e)
            raise
        _log_usage(conn, "assistant", s["model"], resp.usage)
        cost += cost_usd(s["model"], resp.usage)
        if resp.stop_reason == "refusal":
            break
        if resp.stop_reason != "tool_use":
            break
        if _spent_today(conn) >= float(s["daily_cost_cap_usd"]):
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                used.append({"tool": b.name, "input": b.input})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": run_tool(conn, b.name, b.input)})
        messages.append({"role": "user", "content": results})
    text = "\n".join(b.text for b in (resp.content if resp else []) if getattr(b, "type", None) == "text").strip()
    if resp is not None and resp.stop_reason == "refusal":
        text = text or "The model declined to answer this question."
    if resp is not None and resp.stop_reason == "tool_use":
        text = text or "I ran out of tool rounds before finishing; please ask a narrower question."
    _store(conn, cid, question, text, "claude", getattr(resp, "model", s["model"]), used, round(cost, 6))
    return {"conversation_id": cid, "answer": text, "mode": "claude", "model": getattr(resp, "model", s["model"]),
            "tools_used": used, "cost_usd": round(cost, 6), "stop_reason": getattr(resp, "stop_reason", None)}


def conversations(conn, limit=30) -> list:
    return [dict(zip(("conversation_id", "title", "created_at", "updated_at", "messages"), r)) for r in conn.execute(
        "SELECT c.conversation_id, c.title, c.created_at, c.updated_at, (SELECT COUNT(*) FROM assistant_message m WHERE "
        "m.conversation_id=c.conversation_id) FROM assistant_conversation c ORDER BY c.updated_at DESC LIMIT ?",
        (int(limit),))]


def conversation(conn, cid) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT asked_at, question, answer, mode, model, tools_json, cost_usd FROM "
                                          "assistant_message WHERE conversation_id=? ORDER BY id", (cid,))]
    if not rows:
        raise LookupError(f"no conversation {cid}")
    for r in rows:
        r["tools"] = json.loads(r.pop("tools_json") or "[]")
    return {"conversation_id": cid, "messages": rows}
