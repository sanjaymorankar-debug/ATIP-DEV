"""
ATIP — Portfolio Health Score
=============================
Implements the architecture doc's section 11:

    PHS = 0.25·Diversification + 0.20·Risk + 0.15·Drawdown
        + 0.15·Quality + 0.15·Allocation + 0.10·Performance

This was the one dashboard section with a published formula and no
implementation at all. Every input already exists in the database — holdings
(portfolio_holdings, synced from Dhan or Zerodha), per-stock ATIP/CRI scores
(ai_scores), beta (ai_scores.beta_1y) and the market regime (market_health) —
so nothing new is fetched.

Each component is normalised to 0–100 and only the components that have data
are weighted, so a portfolio missing (say) beta for every holding still gets a
meaningful score from the rest rather than a silently diluted one.

Holdings exist only for dates the portfolio sync actually ran, which lags the
scored dates whenever that job fails. Rather than disappear, the score falls
back to the most recent date that has holdings and returns it as `as_of` with a
`stale` flag, so callers can label the figure instead of showing nothing.

Returns None only when there are no holdings at all on or before the requested
date — an empty portfolio has no health, and reporting 50 would read as
"average" rather than "nothing to measure".
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

# Interpretation bands, mirroring the doc's Market Health bands so the two
# read on the same scale.
BANDS = [(80, "STRONG"), (60, "HEALTHY"), (40, "FAIR"), (20, "WEAK"), (0, "CRITICAL")]


def _band(score):
    for floor, label in BANDS:
        if score >= floor:
            return label
    return "CRITICAL"


def _norm(val, lo, hi, invert=False):
    if val is None:
        return None
    pct = max(0.0, min(100.0, (val - lo) / (hi - lo) * 100)) if hi != lo else 50.0
    return round(100.0 - pct if invert else pct, 2)


def compute_phs(trade_date=None, conn=None) -> dict | None:
    own = conn is None
    if own:
        conn = get_connection()
    if trade_date is None:
        trade_date = date.today()
    try:
        # Read the scores from ai_scores rather than from portfolio_holdings.
        # The sync is supposed to enrich holdings with atip_score/cri/beta_1y,
        # but it is a separate job that can fail (it had failed 9 times in this
        # database, leaving all three NULL for every holding while ai_scores
        # held them for the same symbol and date). COALESCE prefers the synced
        # value when present and falls back to the authoritative table, so PHS
        # doesn't silently lose its Risk and Quality components to an unrelated
        # job failure.
        # Columns are listed explicitly rather than using h.* — with h.* the
        # aliased COALESCE columns below become DUPLICATE keys (h.* already
        # emits atip_score/cri/beta_1y) and dict(row) then keeps the NULL one,
        # silently defeating the whole point of the join.
        # Holdings only exist for dates the portfolio sync actually ran, which
        # lags the scored dates whenever that job fails. Rather than vanish,
        # fall back to the most recent date that HAS holdings and report it as
        # `as_of` so the caller can label the figure honestly.
        as_of = str(trade_date)
        have = conn.execute("SELECT COUNT(*) FROM portfolio_holdings WHERE date=?", (as_of,)).fetchone()[0]
        if not have:
            row = conn.execute("SELECT MAX(date) FROM portfolio_holdings WHERE date<=?",
                               (as_of,)).fetchone()
            if not row or not row[0]:
                return None
            as_of = str(row[0])

        holdings = [dict(r) for r in conn.execute("""
            SELECT h.symbol, h.qty, h.avg_price, h.cmp, h.current_val, h.pnl_pct,
                   h.weight_pct, h.sector,
                   COALESCE(h.atip_score, s.atip_score) AS atip_score,
                   COALESCE(h.cri,        s.cri)        AS cri,
                   COALESCE(h.beta_1y,    s.beta_1y)    AS beta_1y
            FROM portfolio_holdings h
            LEFT JOIN ai_scores s ON s.symbol = h.symbol AND s.date = h.date
            WHERE h.date = ?
        """, (as_of,)).fetchall()]
        if not holdings:
            return None

        weights = {r["variable"]: r["weight"] for r in conn.execute(
            "SELECT variable,weight FROM weight_config WHERE index_name='PHS' AND active=1").fetchall()}
        if not weights:      # table not seeded — fall back to the doc's figures
            weights = {"Diversification": .25, "Risk": .20, "Drawdown": .15,
                       "Quality": .15, "Allocation": .15, "Performance": .10}

        # Deliberately the REQUESTED date's regime, not the holdings date's.
        # Allocation asks "is this book sized right for current conditions", so
        # it should read today's regime even when the holdings are older.
        mh = conn.execute("SELECT mh_score,regime FROM market_health WHERE date<=? "
                          "ORDER BY date DESC LIMIT 1", (str(trade_date),)).fetchone()
        regime = mh["regime"] if mh else "NEUTRAL"
        mh_score = (mh["mh_score"] if mh else 50) or 50

        n = len(holdings)
        vals = [h.get("current_val") or 0 for h in holdings]
        total = sum(vals) or 1.0
        c = {}

        # ── Diversification: how concentrated is the book? ────────────────────
        # Herfindahl index over position weights. 1/n (perfectly even) scores
        # 100; everything in one name scores 0. Also penalises a book with very
        # few positions, since 3 evenly-split holdings are not diversified.
        hhi = sum((v / total) ** 2 for v in vals) if total else 1.0
        even = 1.0 / n
        spread = _norm(hhi, 1.0, even, invert=True) if n > 1 else 0.0
        breadth = _norm(min(n, 20), 1, 20)
        c["Diversification"] = round((spread or 0) * 0.6 + (breadth or 0) * 0.4, 2)

        # ── Risk: portfolio beta and crash-risk exposure ─────────────────────
        # PortfolioBeta = Σ(weight_i × beta_i), the doc's definition. Beta near
        # 1.0 is neutral; well above 1 is the risk being measured.
        betas = [(h.get("beta_1y"), (h.get("current_val") or 0) / total)
                 for h in holdings if h.get("beta_1y") is not None]
        pbeta = sum(b * w for b, w in betas) / sum(w for _, w in betas) if betas else None
        cris = [h["cri"] for h in holdings if h.get("cri") is not None]
        cri_avg = sum(cris) / len(cris) if cris else None
        parts = [p for p in (_norm(pbeta, 0.5, 2.0, invert=True),
                             _norm(cri_avg, 0, 100, invert=True)) if p is not None]
        if parts:
            c["Risk"] = round(sum(parts) / len(parts), 2)

        # ── Drawdown: how far below its peak is each position? ───────────────
        # Uses avg_price vs cmp per holding, value-weighted. A book sitting at
        # its highs scores 100; one deeply underwater scores 0.
        dds = []
        for h in holdings:
            avg, cmp_ = h.get("avg_price"), h.get("cmp")
            if avg and cmp_ and avg > 0:
                dds.append((min(0.0, (cmp_ - avg) / avg * 100), (h.get("current_val") or 0) / total))
        if dds:
            wdd = sum(d * w for d, w in dds) / (sum(w for _, w in dds) or 1)
            c["Drawdown"] = _norm(wdd, -30, 0)

        # ── Quality: mean ATIP score of what you actually hold ───────────────
        atips = [h["atip_score"] for h in holdings if h.get("atip_score") is not None]
        if atips:
            c["Quality"] = round(sum(atips) / len(atips), 2)

        # ── Allocation: is position sizing appropriate to the regime? ────────
        # In a weak regime a concentrated, fully-deployed book is penalised; in
        # a strong regime it isn't. Largest position weight is the proxy.
        top_w = max((v / total for v in vals), default=1.0) * 100
        ceiling = 25 if regime in ("STRONG_BULL", "BULL") else 15 if regime == "NEUTRAL" else 10
        c["Allocation"] = _norm(top_w, ceiling * 2.5, ceiling, invert=False) if top_w > ceiling \
            else 100.0
        if c["Allocation"] is None:
            c["Allocation"] = 50.0

        # ── Performance: unrealised P&L, value-weighted ──────────────────────
        pnls = [(h.get("pnl_pct"), (h.get("current_val") or 0) / total)
                for h in holdings if h.get("pnl_pct") is not None]
        if pnls:
            wp = sum(p * w for p, w in pnls) / (sum(w for _, w in pnls) or 1)
            c["Performance"] = _norm(wp, -20, 30)

        tw = sum(weights[k] for k in c if k in weights and c[k] is not None)
        score = round(sum(c[k] * weights[k] for k in c if k in weights and c[k] is not None) / tw, 2) \
            if tw else 50.0

        out = {"date": as_of, "requested_date": str(trade_date),
               "stale": as_of != str(trade_date),
               "phs": score, "band": _band(score),
               "holdings": n, "portfolio_beta": round(pbeta, 3) if pbeta else None,
               "regime": regime, "mh_score": mh_score, "components": c,
               "weight_covered": round(tw, 3)}
        log.info(f"  ✓ Portfolio Health: {score} ({out['band']}) over {n} holdings"
                 + (f", beta {out['portfolio_beta']}" if out['portfolio_beta'] else "")
                 + (f"  [holdings as of {as_of}, requested {trade_date}]" if out["stale"] else ""))
        return out
    finally:
        if own:
            conn.close()


def store_phs(trade_date=None) -> dict | None:
    """Compute and persist to market_health.portfolio_health for the dashboard."""
    conn = get_connection()
    try:
        res = compute_phs(trade_date, conn=conn)
        if not res:
            log.info("  Portfolio Health: no holdings for this date — nothing to score")
            return None
        _ensure_column(conn)
        conn.execute("UPDATE market_health SET portfolio_health=? WHERE date=?",
                     (res["phs"], res["date"]))
        if conn.total_changes == 0:
            conn.execute("INSERT OR IGNORE INTO market_health(date,portfolio_health) VALUES(?,?)",
                         (res["date"], res["phs"]))
        conn.commit()
        log_job("portfolio_health", "SUCCESS", res["holdings"], run_date=trade_date)
        return res
    except Exception as e:
        conn.rollback()
        log.error(f"  ✗ Portfolio Health failed: {e}")
        log_job("portfolio_health", "FAILED", 0, error=e, run_date=trade_date)
        return None
    finally:
        conn.close()


def _ensure_column(conn):
    """Additive migration, same pattern as db/schema.py."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(market_health)").fetchall()}
    except Exception:
        return
    if "portfolio_health" not in cols:
        try:
            conn.execute("ALTER TABLE market_health ADD COLUMN portfolio_health REAL")
            conn.commit()
            log.info("  ✓ market_health migrated — added column: portfolio_health")
        except Exception as e:
            log.warning(f"  market_health migration skipped portfolio_health: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP Portfolio Health Score")
    ap.add_argument("--date")
    ap.add_argument("--store", action="store_true", help="persist to market_health")
    args = ap.parse_args()
    td = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    res = store_phs(td) if args.store else compute_phs(td)
    if not res:
        print("No holdings for that date.")
    else:
        print(f"\n  Portfolio Health: {res['phs']}  ({res['band']})")
        print(f"  holdings {res['holdings']}   portfolio beta {res['portfolio_beta']}   "
              f"regime {res['regime']}")
        for k, v in res["components"].items():
            print(f"     {k:16} {v}")
        print(f"  weight covered: {res['weight_covered']}\n")
