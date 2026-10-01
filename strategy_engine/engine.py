"""
Generate a strategy's decisions for one date (the signal-engine integration).

    market data -> features (bars, ATIP scores from the signal engine, regime)
      -> strategy version's evaluator -> StrategyDecisions -> PositionIntents

Decisions use the version's own parameters -- a parameter change is a new
version, so every stored decision names exactly what produced it. Held
positions come from the W1 books (portfolio/pnl.py): the PAPER book for
PAPER / READY strategies and requests, the LIVE book for ACTIVE ones. The
broker holdings do not say when a position was opened, so max_hold_sessions
is not applied to live decisions (it is in backtests).

Output: one StrategyDecision per evaluated symbol (strategy_decision) and a
PositionIntent for each decision that would change a position
(strategy_position_intent). An intent's quantity is indicative only -- the
W1 sizer (orders/risk.py size_position) on PAPER equity for the PAPER book;
None for the LIVE book, because the broker is not asked from here.

Nothing here places, queues or authorises an order: every intent is stored
with authorization_status NOT_AUTHORIZED. There is no code path from this
package to a broker; execution is W4.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import date, datetime, timedelta

from db.schema import get_connection
from strategy_engine import lifecycle, registry
from strategy_engine.decisions import A_ADD, A_BUY, A_REDUCE, NOT_AUTHORIZED, intent_for
from strategy_engine.definition import specs
from strategy_engine.kinds import EvalEnv, make_evaluator, session_ordinal
from strategy_engine.params import resolve
from strategy_engine.regime import get_provider

log = logging.getLogger("atip.strategy_engine")

WARMUP_DAYS = 420          # enough calendar days for 250-bar features (sma_200, 52-week ranges)


def latest_session(conn) -> date | None:
    r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE source IN ('dhan','bhavcopy')").fetchone()[0]
    return date.fromisoformat(str(r)[:10]) if r else None


def _universe(conn, defn) -> tuple:
    u = defn.get("universe") or {"type": "tracked_current"}
    if u.get("type") == "symbols":
        return tuple(sorted(set(s.upper() for s in u["symbols"])))
    from backtest.data import tracked_universe
    return tracked_universe(conn).symbols


def _held(conn, book: str, as_of: date) -> dict:
    from portfolio.pnl import positions
    held = {p["symbol"]: {"qty": p["qty"], "entry_price": p["avg_price"], "held_sessions": None}
            for p in positions(conn, book, as_of)}
    if book == "PAPER":                    # W30: short legs held in the paper futures book
        try:
            from execution.futures_paper import held_shorts
            for s, v in held_shorts(conn).items():
                held.setdefault(s, v)
        except Exception as e:
            log.debug(f"  futures shorts unavailable: {e}")
    return held


def build_env(conn, symbols, as_of: date, inputs=None) -> EvalEnv:
    from backtest.data import PriceHistory, ScoresHistory
    history = PriceHistory.load(conn, symbols, as_of, as_of, warmup_days=WARMUP_DAYS)
    scores = ScoresHistory(conn, as_of - timedelta(days=10), as_of)
    regime = get_provider(conn, as_of - timedelta(days=10), as_of)
    ml = None
    if inputs is None or "ml" in inputs:           # W5: stored ML predictions, point in time
        try:
            from ml.strategy_features import MLPredictionHistory
            ml = MLPredictionHistory(conn, as_of - timedelta(days=10), as_of)
        except Exception as e:
            log.warning(f"  ML predictions unavailable to strategies: {e}")
    quant = None
    if inputs is None or "quant" in inputs:        # W6: stored factor / composite scores + events
        try:
            from quant.strategy_features import QuantHistory
            quant = QuantHistory(conn, as_of - timedelta(days=10), as_of)
        except Exception as e:
            log.warning(f"  quant scores unavailable to strategies: {e}")
    bench = {}
    for d, c in conn.execute("SELECT date, close FROM prices_daily WHERE symbol='NIFTY50' AND date>=? AND date<=?",
                             (str(as_of - timedelta(days=WARMUP_DAYS)), str(as_of))):
        bench[d if isinstance(d, date) else date.fromisoformat(str(d)[:10])] = c
    return EvalEnv(history, symbols, scores, regime, bench, ml, quant)


def _capital(conn, book: str, as_of: date):
    """Capital for indicative sizing: PAPER equity for the PAPER book. For LIVE the
    broker is never asked from here, so there is no capital and no quantity."""
    if book != "PAPER":
        return None
    try:
        from portfolio.pnl import portfolio_summary
        return portfolio_summary(conn, "PAPER", as_of).get("equity")
    except Exception:
        return None


def _quantity(dec, capital, close):
    """Indicative quantity for a BUY / ADD intent via the W1 sizer (orders/risk.py
    size_position, pure). None when it cannot be sized; W4 confirms sizing."""
    rq = (dec.features or {}).get("rebalance_qty")          # W25 PF-06 reweight
    if rq and dec.action in (A_ADD, A_REDUCE):
        return int(rq)
    if capital is None or not close or dec.action not in (A_BUY, A_ADD):
        return None
    try:
        from orders.risk import size_position
        r = size_position(close, dec.stop_price, capital=capital, max_position_pct=dec.target_position_pct)
        return int(r["quantity"]) or None
    except Exception:
        return None


def generate_decisions(strategy_id: str, version: str | None = None, as_of=None, book: str = "PAPER",
                       store: bool = True, conn=None) -> dict:
    """
    One decision run: a StrategyDecision per evaluated symbol and a
    PositionIntent (NOT_AUTHORIZED) per decision that would change a position.
    A failure is recorded (run status FAILED + an ERROR event) and re-raised.
    """
    own = conn is None
    conn = conn or get_connection()
    run_id = uuid.uuid4().hex[:16]
    ver = version
    try:
        v = registry.get_version(conn, strategy_id, version)
        if not v:
            raise ValueError(f"no strategy {strategy_id} {version or '(current)'}")
        defn = v["definition"]
        ver = defn["version"]
        params = resolve(specs(defn))
        as_of = date.fromisoformat(str(as_of)) if as_of else latest_session(conn)
        if as_of is None:
            raise ValueError("no price data to decide on")
        if book not in ("PAPER", "LIVE"):
            raise ValueError("book must be PAPER or LIVE")
        held = _held(conn, book, as_of)
        symbols = tuple(sorted(set(_universe(conn, defn)) | set(held)))
        env = build_env(conn, symbols, as_of)
        ev = make_evaluator(defn, params, registry.loader(conn))
        decisions = ev.decide(env, as_of, held, session_ordinal(as_of))
        capital = _capital(conn, book, as_of)
        intents = []
        for d in decisions:
            it = intent_for(d, _quantity(d, capital, (d.features or {}).get("close")))
            if it:
                intents.append(it)
        counts = Counter(d.action for d in decisions)
        n_eval = sum(1 for s in symbols if env.context(s, as_of) is not None)
        if store:
            now = datetime.now()
            conn.execute("INSERT INTO strategy_decision_run (run_id,strategy_id,version,as_of,book,status,params_json,"
                         "n_universe,n_evaluated,counts_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, strategy_id, ver, str(as_of), book, "SUCCESS", json.dumps(params, sort_keys=True),
                          len(symbols), n_eval, json.dumps(dict(counts)), now))
            # a re-run of the same (strategy, version, date) replaces that day's decisions and intents --
            # unless the W4 risk engine has already acted on one: those stay, for the audit trail
            acted = conn.execute("SELECT COUNT(*) FROM strategy_position_intent WHERE strategy_id=? AND version=? "
                                 "AND as_of=? AND authorization_status<>'NOT_AUTHORIZED'",
                                 (strategy_id, ver, str(as_of))).fetchone()[0]
            if acted:
                raise ValueError(f"{strategy_id} {ver} decisions for {as_of} already have {acted} intent(s) "
                                 f"evaluated by the risk engine; they are kept for the audit trail, not replaced")
            conn.execute("DELETE FROM strategy_position_intent WHERE strategy_id=? AND version=? AND as_of=?",
                         (strategy_id, ver, str(as_of)))
            conn.execute("DELETE FROM strategy_decision WHERE strategy_id=? AND version=? AND as_of=?",
                         (strategy_id, ver, str(as_of)))
            conn.executemany(
                "INSERT INTO strategy_decision (decision_id,run_id,strategy_id,version,as_of,timestamp,symbol,decision,"
                "action,confidence,score,regime,reasons_json,parameters_json,risk_requirement,target_position_pct,"
                "stop_price,target_price,max_hold_sessions,blocked_reason,features_json,reason_codes_json,"
                "signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(d.decision_id, run_id, d.strategy_id, d.strategy_version, str(d.as_of), d.timestamp, d.symbol,
                  d.decision, d.action, d.confidence, d.score, d.regime, json.dumps(d.reasons),
                  json.dumps(d.parameters, default=str, sort_keys=True), d.risk_requirement, d.target_position_pct,
                  d.stop_price, d.target_price, d.max_hold_sessions, d.blocked_reason,
                  json.dumps(d.features, default=str), json.dumps(d.codes()), d.signal_source)
                 for d in decisions])
            conn.executemany(
                "INSERT INTO strategy_position_intent (intent_id,decision_id,strategy_id,version,as_of,timestamp,symbol,"
                "side,action,target_position_pct,quantity,stop_price,target_price,max_hold_sessions,confidence,reason,"
                "risk_requirement,authorization_status,created_at,entry_reference,book) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(i.intent_id, i.decision_id, i.strategy_id, i.strategy_version, str(as_of), i.timestamp, i.symbol,
                  i.side, i.action, i.target_position_pct, i.quantity, i.stop_price, i.target_price,
                  i.max_hold_sessions, i.confidence, i.reason, i.risk_requirement, NOT_AUTHORIZED, now,
                  i.entry_reference, book)
                 for i in intents])
            conn.commit()
        return {"run_id": run_id if store else None, "strategy_id": strategy_id, "version": ver,
                "as_of": str(as_of), "book": book, "counts": dict(counts), "n_universe": len(symbols),
                "n_evaluated": n_eval, "decisions": [d.as_dict() for d in decisions],
                "intents": [i.as_dict() for i in intents]}
    except Exception as e:
        if store:
            try:
                conn.rollback()
                conn.execute("INSERT INTO strategy_decision_run (run_id,strategy_id,version,as_of,book,status,error,"
                             "created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (run_id, strategy_id, ver, str(as_of) if as_of else None, book, "FAILED", str(e)[:2000],
                              datetime.now()))
                lifecycle.log_event(conn, strategy_id, "ERROR", f"decision run failed: {e}", ver,
                                    {"run_id": run_id, "as_of": str(as_of) if as_of else None, "book": book})
            except Exception:
                pass
        raise
    finally:
        if own:
            conn.close()


def run_scheduled_decisions(trade_date=None) -> dict:
    """Decisions for every strategy in PAPER / READY / ACTIVE (run_job compatible)."""
    conn = get_connection()
    try:
        registry.sync_library(conn)
        rows = conn.execute(f"SELECT strategy_id, status FROM strategy WHERE status IN "
                            f"({','.join('?' * len(lifecycle.DECISION_STATES))})",
                            lifecycle.DECISION_STATES).fetchall()
    finally:
        conn.close()
    done, failed = {}, {}
    for sid, status in rows:
        try:
            r = generate_decisions(sid, as_of=trade_date, book="LIVE" if status == "ACTIVE" else "PAPER")
            done[sid] = r["counts"]
        except Exception as e:
            failed[sid] = str(e)
            log.warning(f"  strategy {sid}: decisions failed: {e}")
    n = sum(sum(c.values()) for c in done.values())
    if not rows:            # nothing in PAPER / READY / ACTIVE: not an EMPTY run (run_job logs the status)
        return {"status": "SKIPPED", "rows": 0, "strategies": {}, "failed": {},
                "reason": "no strategy in PAPER / READY / ACTIVE"}
    return {"status": "FAILED" if failed and not done else "SUCCESS", "rows": n,
            "strategies": done, "failed": failed, "error": "; ".join(f"{k}: {v}" for k, v in failed.items()) or None}
