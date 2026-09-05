"""
ATIP — Prediction Writer
========================
Turns each day's ai_scores row into a *tradeable, measurable* prediction:
entry price, stop loss, target_1 / target_2, risk:reward and position size.

WHY THIS EXISTS
    The predictions table was in the schema from day one but nothing in the
    codebase ever wrote to it. scores/accuracy.py reads FROM predictions,
    so with that table empty the accuracy tracker could never produce a single
    row — which meant ACS.HistoricalAccuracy silently sat on its 50.0 fallback
    forever, and there was no way to answer "what is this system's actual hit
    rate?". This module is the missing half.

    It also fills predictions.risk_reward and predictions.position_size_pct,
    two more columns that existed in the schema but were never populated
    (Position Sizing / Trade Planner "R:R" gaps).

STRATEGY ENCODED HERE
    Targets default to +3% (target_1) and +6% (target_2) — a fixed-percentage
    exit, not the ATR-multiple exit that the Trade-of-the-Day card uses. The
    stop is ATR-based (volatility-adaptive) but clamped into a sane percentage
    band, because a raw 1.5x ATR stop on a 60%-volatility smallcap can sit 9%
    away, which wrecks the risk:reward on a 3% target.

    Every number below is a starting point, not a tuned parameter — change
    them here and re-run the backtester (scores/backtest.py) to see what
    it does to expectancy before trusting it with real money.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

# ── Exit / sizing parameters ────────────────────────────────────────────────
TARGET_1_PCT = 3.0        # first profit target
TARGET_2_PCT = 6.0        # second profit target
STOP_ATR_MULT = 1.5       # stop = entry - (mult x ATR), then clamped below
STOP_MIN_PCT = 2.0        # never place the stop tighter than this
STOP_MAX_PCT = 6.0        # never place the stop wider than this
RISK_PER_TRADE_PCT = 1.0  # capital fraction risked per trade
MAX_POSITION_PCT = 10.0   # cap on any single position, whatever the risk math says

# Only these signals get a directional trade plan. HOLD/WAIT rows are still
# written (so accuracy can measure whether sitting out was right) but with no
# target/stop, which also keeps them out of hit_target_1 / hit_stop_loss stats.
ACTIONABLE = ("BUY", "SELL")


def compute_trade_plan(signal: str, entry: float, atr: float | None) -> dict:
    """
    Entry/stop/targets/R:R/size for one prediction.

    Returns all-None target/stop fields for non-actionable signals rather than
    inventing levels for a trade the engine didn't actually recommend.
    """
    if not entry or entry <= 0 or signal not in ACTIONABLE:
        return {"stop_loss": None, "target_1": None, "target_2": None,
                "risk_reward": None, "position_size_pct": None}

    # ATR-based stop distance, clamped into the percentage band.
    atr_pct = (atr / entry * 100) if atr else None
    raw_stop_pct = (atr_pct * STOP_ATR_MULT) if atr_pct else STOP_MIN_PCT
    stop_pct = max(STOP_MIN_PCT, min(raw_stop_pct, STOP_MAX_PCT))

    if signal == "BUY":
        stop_loss = round(entry * (1 - stop_pct / 100), 2)
        target_1 = round(entry * (1 + TARGET_1_PCT / 100), 2)
        target_2 = round(entry * (1 + TARGET_2_PCT / 100), 2)
    else:  # SELL — mirrored, so a short/exit plan is measurable the same way
        stop_loss = round(entry * (1 + stop_pct / 100), 2)
        target_1 = round(entry * (1 - TARGET_1_PCT / 100), 2)
        target_2 = round(entry * (1 - TARGET_2_PCT / 100), 2)

    risk_reward = round(TARGET_1_PCT / stop_pct, 2)
    # Risk-based sizing: risking RISK_PER_TRADE_PCT of capital with the stop
    # stop_pct away implies this position size; capped so one name can't
    # dominate the book when the stop is very tight.
    position_size_pct = round(min(MAX_POSITION_PCT, RISK_PER_TRADE_PCT / stop_pct * 100), 2)

    return {"stop_loss": stop_loss, "target_1": target_1, "target_2": target_2,
            "risk_reward": risk_reward, "position_size_pct": position_size_pct}


def _reasoning(row: dict) -> str:
    """Short human-readable why, from the factors the engine already ranked."""
    bits = [row.get(f"top_factor_{i}") for i in (1, 2, 3)]
    bits = [b for b in bits if b]
    extra = []
    if row.get("regime"):
        extra.append(f"regime={row['regime']}")
    if row.get("cri") is not None:
        extra.append(f"CRI={row['cri']:.0f}")
    return " · ".join(bits + extra)[:400]


def write_predictions(trade_date=None, conn=None) -> dict:
    """
    Write one prediction row per scored symbol for trade_date.

    Idempotent: re-running for the same date updates the existing rows rather
    than duplicating (UNIQUE(pred_date,symbol) + ON CONFLICT), so it's safe to
    call from the pipeline and again by hand.
    """
    if trade_date is None:
        trade_date = date.today()
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    result = {"date": str(trade_date), "rows": 0, "actionable": 0, "status": "SUCCESS"}
    try:
        rows = conn.execute("""
            SELECT s.*, p.close AS entry_price, t.atr_14
            FROM ai_scores s
            LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date
            LEFT JOIN technical_indicators t ON t.symbol=s.symbol AND t.date=s.date
            WHERE s.date=?
        """, (str(trade_date),)).fetchall()

        written = actionable = 0
        for r in rows:
            row = dict(r)
            entry = row.get("entry_price")
            if not entry:
                continue  # no price to anchor the plan to — nothing measurable
            signal = row.get("signal") or "WAIT"
            plan = compute_trade_plan(signal, entry, row.get("atr_14"))
            if signal in ACTIONABLE:
                actionable += 1
            conn.execute("""
                INSERT INTO predictions (
                    pred_date, symbol, signal, atip_score, vpi, zpi, mri, cri, acs,
                    entry_price, stop_loss, target_1, target_2, risk_reward,
                    position_size_pct, confidence, reasoning, regime, is_tod
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pred_date,symbol) DO UPDATE SET
                    signal=excluded.signal, atip_score=excluded.atip_score,
                    vpi=excluded.vpi, zpi=excluded.zpi, mri=excluded.mri,
                    cri=excluded.cri, acs=excluded.acs,
                    entry_price=excluded.entry_price, stop_loss=excluded.stop_loss,
                    target_1=excluded.target_1, target_2=excluded.target_2,
                    risk_reward=excluded.risk_reward,
                    position_size_pct=excluded.position_size_pct,
                    confidence=excluded.confidence, reasoning=excluded.reasoning,
                    regime=excluded.regime, is_tod=excluded.is_tod
            """, (
                str(trade_date), row["symbol"], signal, row.get("atip_score"),
                row.get("vpi"), row.get("zpi"), row.get("mri"), row.get("cri"),
                row.get("acs"), entry, plan["stop_loss"], plan["target_1"],
                plan["target_2"], plan["risk_reward"], plan["position_size_pct"],
                row.get("acs"), _reasoning(row), row.get("regime"),
                row.get("is_tod") or 0,
            ))
            written += 1

        conn.commit()
        result["rows"], result["actionable"] = written, actionable
        log.info(f"  ✓ Predictions: {written} written ({actionable} actionable BUY/SELL)")
        log_job("predictions", "SUCCESS", written, run_date=trade_date)
    except Exception as e:
        conn.rollback()
        result["status"] = "FAILED"
        log.error(f"  ✗ Predictions failed: {e}")
        log_job("predictions", "FAILED", 0, error=e, run_date=trade_date)
    finally:
        if own_conn:
            conn.close()
    return result


def backfill_predictions() -> dict:
    """
    Write predictions for every date that already has ai_scores but no
    predictions — recovers measurable history from scores that were computed
    before this module existed.
    """
    conn = get_connection()
    try:
        dates = [r["date"] for r in conn.execute("""
            SELECT DISTINCT s.date FROM ai_scores s
            WHERE NOT EXISTS (SELECT 1 FROM predictions p WHERE p.pred_date=s.date)
            ORDER BY s.date
        """).fetchall()]
    finally:
        conn.close()
    if not dates:
        log.info("  Nothing to backfill — every scored date already has predictions")
        return {"dates": 0, "rows": 0}
    total = 0
    for d in dates:
        total += write_predictions(d).get("rows", 0)
    log.info(f"  ✓ Backfilled {total} predictions across {len(dates)} dates")
    return {"dates": len(dates), "rows": total}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Write ATIP predictions (entry/SL/targets/size) from ai_scores")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill", action="store_true", help="write predictions for all scored dates that lack them")
    args = ap.parse_args()
    if args.backfill:
        print(backfill_predictions())
    else:
        td = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
        print(write_predictions(td))
