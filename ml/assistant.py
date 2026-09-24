"""
Read-only "why" answers -- the foundation for a natural-language assistant.

ATIP's only existing LLM use is news classification (data/news.py, Anthropic);
there is no chat component to integrate. This module assembles the facts an
assistant would answer from, deterministically, from stored rows:

    explain_symbol(symbol, as_of)  ATIP scores + top factors, the market regime,
                                   ML predictions with top features, strategy
                                   decisions (reasons, codes), intents and
                                   their risk verdicts
    answer(question, symbol)       picks the relevant facts for simple questions
                                   ("why this score", "which factors", "which
                                   strategy", "what regime") and returns them as text

It never calls an order, risk or execution function, and has no write path.
A later LLM layer can take explain_symbol()'s output as its only context.
"""

from __future__ import annotations

import json
from datetime import date


def _one(conn, q, args):
    r = conn.execute(q, args).fetchone()
    return dict(r) if r else None


def explain_symbol(conn, symbol: str, as_of=None) -> dict:
    symbol = symbol.upper()
    if not as_of:
        r = conn.execute("SELECT MAX(date) FROM ai_scores WHERE symbol=?", (symbol,)).fetchone()
        as_of = r[0] if r and r[0] else str(date.today())
    as_of = str(as_of)[:10]
    scores = _one(conn, "SELECT * FROM ai_scores WHERE symbol=? AND date=?", (symbol, as_of))
    regime = _one(conn, "SELECT date, regime, mh_score, vix_level, pct_advancing FROM market_health WHERE date<=? "
                        "ORDER BY date DESC LIMIT 1", (as_of,))
    preds = []
    for r in conn.execute("SELECT model_id, model_version, version_status, prediction, confidence, ml_score, "
                          "explanation_json FROM ml_prediction WHERE symbol=? AND as_of=?", (symbol, as_of)):
        d = dict(r); d["explanation"] = json.loads(d.pop("explanation_json") or "null"); preds.append(d)
    decs = []
    for r in conn.execute("SELECT strategy_id, version, decision, action, confidence, score, regime, reasons_json, "
                          "reason_codes_json, signal_source FROM strategy_decision WHERE symbol=? AND as_of=?",
                          (symbol, as_of)):
        d = dict(r)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
        decs.append(d)
    intents = [dict(r) for r in conn.execute(
        "SELECT i.intent_id, i.strategy_id, i.side, i.action, i.authorization_status, r.risk_status, "
        "r.rejection_reason FROM strategy_position_intent i LEFT JOIN risk_decision r "
        "ON r.risk_decision_id=i.risk_decision_id WHERE i.symbol=? AND i.as_of=?", (symbol, as_of))]
    return {"symbol": symbol, "as_of": as_of, "atip_scores": scores, "market_regime": regime,
            "ml_predictions": preds, "strategy_decisions": decs, "intents": intents,
            "note": "informational only; no trading action is available from this endpoint"}


def answer(conn, question: str, symbol: str, as_of=None) -> dict:
    f = explain_symbol(conn, symbol, as_of)
    q = (question or "").lower()
    lines = []
    s = f["atip_scores"]
    if any(w in q for w in ("score", "why")) and s:
        lines.append(f"{f['symbol']} on {f['as_of']}: ATIP score {s.get('atip_score')}, signal {s.get('signal')}; "
                     f"top factors {', '.join(str(s.get(k)) for k in ('top_factor_1', 'top_factor_2', 'top_factor_3') if s.get(k))}.")
    if any(w in q for w in ("factor", "feature", "prediction", "model", "why")):
        for p in f["ml_predictions"]:
            ex = p.get("explanation") or {}
            lines.append(f"Model {p['model_id']} {p['model_version']} ({p['version_status']}): {p['prediction']} "
                         f"(confidence {p['confidence']}, ML score {p['ml_score']}); top features "
                         f"{', '.join(ex.get('top_features') or []) or 'n/a'}.")
    if any(w in q for w in ("strategy", "signal", "why")):
        for d in f["strategy_decisions"]:
            lines.append(f"Strategy {d['strategy_id']} {d['version']}: {d['decision']} ({', '.join(d['reason_codes'])}) — "
                         f"{'; '.join(d['reasons'])[:300]}")
    if "regime" in q and f["market_regime"]:
        m = f["market_regime"]
        lines.append(f"Market Health regime on {m['date']}: {m['regime']} (score {m['mh_score']}, VIX {m['vix_level']}).")
    if not lines:
        lines.append("No stored facts match that question for this symbol and date.")
    return {"question": question, "answer": "\n".join(lines), "facts": f}
