"""
Portfolio P&L: positions, marks, equity and the daily series (PF-02, PF-13).

One view over the two places ATIP holds positions:

  PAPER  orders/paper.py's paper_position (+ paper_account cash) -- every
         simulated order; realised P&L is kept per symbol
  LIVE   portfolio_holdings, synced from Dhan (or Zerodha) -- the real account;
         the broker reports average cost, so P&L is unrealised only

pnl_daily holds one row per (date, env): equity, day P&L, the running peak and
the drawdown from it. It is what the daily-loss and drawdown limits
(orders/risk.py, RK-07/RK-08) are measured against, and nothing before it
recorded a daily P&L at all: paper_position.realized_pnl is cumulative and
unrealised P&L existed only as a live quote.

Marks: the latest live quote on the day itself, otherwise the session close in
prices_daily; a holding with neither is valued at its broker-reported price.

External cash flows (owner item 8, 16 Nov 2026; portfolio/cash_flows.py): a deposit, withdrawal or transfer is
not P&L. Day P&L = equity - previous equity - the net CONFIRMED flows since the previous row, and a flow scales
the running peak by (equity before + flow) / equity before, so a withdrawal never reads as a loss nor a drawdown
(and a deposit never hides one). With flows on record, risk_state's peak and drawdown are recomputed from the
stored equity series and the flows (so a flow entered later corrects the view without rewriting history); a book
with no confirmed flow keeps the exact arithmetic it had (MAX(peak_equity)) -- PAPER today. A LIVE cash change
that trades cannot explain is stored as a PENDING flow at the 16:49 IST job and counts as P&L until the owner
confirms it (a manual loss at Dhan outside ATIP must not be hidden as a withdrawal).

    python -m portfolio.pnl                   # both environments, now
    python -m portfolio.pnl --record          # store today's pnl_daily rows
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

PAPER, LIVE = "PAPER", "LIVE"
ENVS = (PAPER, LIVE)


def _table_exists(conn, name) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def mark_price(conn, symbol: str, on: date | None = None) -> float | None:
    """The price to value `symbol` at on date `on` (default today)."""
    on = on or date.today()
    if on == date.today() and _table_exists(conn, "live_quotes"):
        r = conn.execute("SELECT ltp FROM live_quotes WHERE symbol=? AND substr(timestamp,1,10)=? AND ltp>0 "
                         "ORDER BY timestamp DESC LIMIT 1", (symbol, str(on))).fetchone()
        if r:
            return float(r[0])
    r = conn.execute("SELECT close FROM prices_daily WHERE symbol=? AND date<=? AND close>0 "
                     "ORDER BY date DESC LIMIT 1", (symbol, str(on))).fetchone()
    return float(r[0]) if r else None


def live_book_date(conn, on: date | None = None):
    """The date whose portfolio_holdings are the LIVE book on `on`: the latest
    successful sync (portfolio_sync), None if it found nothing held. Before
    sync outcomes were recorded, the latest date with holdings."""
    on = on or date.today()
    if _table_exists(conn, "portfolio_sync"):
        r = conn.execute("SELECT date, n_holdings FROM portfolio_sync WHERE status='SUCCESS' AND date<=? "
                         "ORDER BY synced_at DESC LIMIT 1", (str(on),)).fetchone()
        if r:
            return str(r[0]) if (r[1] or 0) > 0 else None
    r = conn.execute("SELECT MAX(date) FROM portfolio_holdings WHERE date<=?", (str(on),)).fetchone()
    return str(r[0]) if r and r[0] else None


def positions(conn, env: str, on: date | None = None) -> list[dict]:
    """Open positions in `env`: symbol, qty, avg_price, mark, value, cost, unrealised."""
    on = on or date.today()
    rows = []
    if env == PAPER:
        if not _table_exists(conn, "paper_position"):
            return []
        rows = [(r[0], r[1], r[2], None) for r in conn.execute(
            "SELECT symbol, quantity, avg_price FROM paper_position WHERE quantity>0")]
    elif env == LIVE:
        latest = live_book_date(conn, on)
        if not latest:
            return []
        rows = [(r[0], r[1], r[2], r[3]) for r in conn.execute(
            "SELECT symbol, qty, avg_price, cmp FROM portfolio_holdings WHERE date=? AND qty>0", (str(latest),))]
    out = []
    for sym, qty, avg, broker_price in rows:
        qty, avg = int(qty or 0), float(avg or 0)
        mark = mark_price(conn, sym, on) or (float(broker_price) if broker_price else None)
        value = qty * mark if mark is not None else None
        cost = qty * avg
        out.append({"symbol": sym, "qty": qty, "avg_price": avg, "mark": mark, "value": value,
                    "cost": cost, "unrealised": (value - cost) if value is not None else None})
    return out


def _paper_cash(conn) -> float | None:
    if not _table_exists(conn, "paper_account"):
        return None
    r = conn.execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
    return float(r[0]) if r else None


def _live_cash() -> float | None:
    """Available funds at Dhan; None when the broker cannot be asked.

    available_balance() returns 0.0 when no field parses (orders/broker.py),
    which as equity would read as the whole cash balance lost -- a drawdown
    that blocks every BUY. Zero is therefore treated as unknown; the cost is
    that a genuinely empty account falls back to the unrealised-P&L basis.

    The real account's funds whatever broker_env says (profit audit 5 Nov 2026, PA-01): under PAPER
    the execution broker is the simulator, and its Rs 3.17 lakh balance was stored as the LIVE book's
    cash from 25 Sep 2026 -- the paper sessions' buys then read as a Rs 1.44 lakh LIVE loss."""
    try:
        from orders.broker import available_balance
        v = available_balance(account=True)
        return float(v) if v else None
    except Exception as e:
        log.debug(f"  live cash unavailable: {e}")
        return None


def portfolio_summary(conn, env: str, on: date | None = None, cash: float | None = None,
                      ask_broker: bool = False) -> dict:
    """
    {env, as_of, positions, n_positions, positions_value, cost, unrealised,
     realised_cum, cash, equity}. equity = cash + positions value, None when
    cash is unknown (LIVE without ask_broker or a cash figure). realised_cum is
    PAPER only -- the broker's holdings carry no realised P&L.
    """
    on = on or date.today()
    pos = positions(conn, env, on)
    valued = [p for p in pos if p["value"] is not None]
    pv = round(sum(p["value"] for p in valued), 2)
    cost = round(sum(p["cost"] for p in pos), 2)
    unreal = round(sum(p["unrealised"] for p in valued), 2)
    realised = None
    if env == PAPER and _table_exists(conn, "paper_position"):
        realised = round(float(conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM paper_position")
                               .fetchone()[0]), 2)
    if cash is None:
        cash = _paper_cash(conn) if env == PAPER else (_live_cash() if ask_broker else None)
    equity = round(cash + pv, 2) if cash is not None else None
    return {"env": env, "as_of": str(on), "positions": pos, "n_positions": len(pos),
            "unvalued": [p["symbol"] for p in pos if p["value"] is None],
            "positions_value": pv, "cost": cost, "unrealised": unreal, "realised_cum": realised,
            "cash": cash, "equity": equity}


def _previous_row(conn, env: str, before: date) -> dict | None:
    r = conn.execute("SELECT * FROM pnl_daily WHERE env=? AND date<? ORDER BY date DESC LIMIT 1",
                     (env, str(before))).fetchone()
    return dict(r) if r else None


def _day_pnl(s: dict, prev: dict | None, flow: float = 0.0) -> float | None:
    """Day P&L against the previous stored row, like for like: the change in equity when both rows
    have one, else the change in unrealised + realised. Profit audit 5 Nov 2026 (PA-01): a row with
    equity used to be set against a previous row's unrealised P&L alone, so the first day a cash
    figure appeared read the whole cash balance as that day's profit.
    `flow` (owner item 8, 16 Nov 2026): the net external cash flow since `prev` (+ deposit, - withdrawal) is
    taken out of the equity change -- it is money moved, not made or lost. The unrealised basis has no cash in it,
    so a flow does not touch it."""
    if not prev:
        return None
    if s.get("equity") is not None and prev.get("equity") is not None:
        return round(s["equity"] - prev["equity"] - float(flow or 0), 2)
    now = (s.get("unrealised") or 0) + (s.get("realised_cum") or 0)
    return round(now - ((prev.get("unrealised") or 0) + (prev.get("realised_cum") or 0)), 2)


def _flows(conn, env: str, through: date) -> list:
    """The confirmed external flows of `env` on or before `through` [(date, amount)]; [] when none are recorded or
    the record cannot be read (then the book is measured exactly as before flows existed -- a withdrawal still
    reads as a loss, which blocks a BUY rather than hiding one)."""
    try:
        from portfolio.cash_flows import confirmed
        return confirmed(conn, env, through)
    except Exception as e:
        log.debug(f"  cash flows unavailable: {e}")
        return []


def _flow_since(flows: list, prev: dict | None) -> float:
    """Net flow dated after the previous row's date (the window that row's equity does not yet carry)."""
    if not prev:
        return 0.0
    after = str(prev["date"])[:10]
    return round(sum(a for d, a in flows if d > after), 2)


def flow_adjusted(conn, env: str, on: date | None = None, equity_now: float | None = None) -> dict:
    """The flow-adjusted equity series of `env` (portfolio/cash_flows.py adjusted_series) from the stored pnl_daily
    equity before `on` and the confirmed flows: per row the flow, the day P&L net of it, the peak and drawdown; with
    `equity_now`, the peak and drawdown as of `on`. Read-only (a recomputed view; stored rows are not rewritten)."""
    from portfolio.cash_flows import adjusted_series
    on = on or date.today()
    rows = [dict(r) for r in conn.execute("SELECT date, equity FROM pnl_daily WHERE env=? AND date<? ORDER BY date",
                                          (env, str(on))).fetchall()] if _table_exists(conn, "pnl_daily") else []
    return adjusted_series(rows, _flows(conn, env, on), equity_now, on)


def risk_state(conn, env: str, cash: float | None = None, ask_broker: bool = False) -> dict:
    """
    Today's P&L and drawdown measured now, for the pre-trade limits:
    {equity, day_pnl, peak_equity, drawdown_pct, prev_date, measurable, external_flow, flow_adjusted,
     pending_flows, unexplained_cash}.
    Day P&L is against the last stored pnl_daily row before today, net of the confirmed external flows since it;
    the peak is the highest stored equity, or today's equity if higher -- scaled by every confirmed flow when the
    book has any (flow_adjusted True), so a withdrawal is never a drawdown. pending_flows (detected, not confirmed)
    and unexplained_cash (a LIVE cash change since the last row that trades cannot explain, measured now) are
    reported, never applied: orders/risk.py names them when a loss limit blocks.
    """
    s = portfolio_summary(conn, env, cash=cash, ask_broker=ask_broker)
    today = date.today()
    prev = _previous_row(conn, env, today) if _table_exists(conn, "pnl_daily") else None
    flows = _flows(conn, env, today)
    flow = _flow_since(flows, prev)
    day_pnl = _day_pnl(s, prev, flow)
    peak = dd = None
    if s["equity"] is not None and flows:
        adj = flow_adjusted(conn, env, today, s["equity"])
        peak, dd = adj["peak"], adj["drawdown_pct"]
    elif s["equity"] is not None:
        stored = conn.execute("SELECT MAX(peak_equity) FROM pnl_daily WHERE env=?", (env,)).fetchone()[0] \
            if _table_exists(conn, "pnl_daily") else None
        peak = max(s["equity"], stored or s["equity"])
        dd = round((peak - s["equity"]) / peak * 100, 2) if peak else None
    pend, unexplained = [], None
    try:
        from portfolio import cash_flows as CF
        pend = CF.pending(conn, env, prev["date"] if prev else None, today)
        unexplained = CF.detect(conn, env, prev, s["cash"], today, flow)
    except Exception as e:
        log.debug(f"  cash-flow detection unavailable: {e}")
    return {"env": env, "equity": s["equity"], "day_pnl": day_pnl, "peak_equity": peak,
            "drawdown_pct": dd, "prev_date": prev["date"] if prev else None,
            "measurable": {"day_pnl": day_pnl is not None, "drawdown_pct": dd is not None},
            "external_flow": flow, "flow_adjusted": bool(flows), "pending_flows": pend,
            "unexplained_cash": unexplained}


def record_daily_pnl(trade_date: date | str | None = None, envs=ENVS) -> dict:
    """
    Store pnl_daily for trade_date (default today) in each environment that has
    anything to record. Re-running a date replaces its row. LIVE asks Dhan for
    available funds only when the date is today; otherwise its equity is None
    and day P&L falls back to the change in unrealised P&L.
    Owner item 8 (16 Nov 2026): day_pnl is net of the confirmed external flows since the previous row and
    peak_equity is scaled by them (portfolio/cash_flows.py); a LIVE cash change that trades cannot explain is
    stored as a PENDING flow (counted only once confirmed, or at once with cash_flows.auto_confirm_detected).
    """
    td = date.fromisoformat(str(trade_date)) if trade_date else date.today()
    conn = get_connection()
    stored = {}
    try:
        for env in envs:
            s = portfolio_summary(conn, env, on=td, ask_broker=(env == LIVE and td == date.today()))
            prev = _previous_row(conn, env, td)
            if not s["positions"] and s["cash"] is None and not prev:
                continue                                   # nothing in this environment, ever
            flows = _flows(conn, env, td)
            flow = _flow_since(flows, prev)
            if env == LIVE:
                try:
                    from portfolio import cash_flows as CF
                    cand = CF.detect(conn, env, prev, s["cash"], td, flow)
                    if cand:
                        rec = CF.record_detected(conn, cand)
                        log.warning(f"  P&L {env} {td}: cash moved {cand['amount']:+,.2f} since {cand['from']} with no "
                                    f"trade -- {rec['status']} external flow #{rec['id']} "
                                    f"(python -m portfolio.cash_flows list --status PENDING)")
                        if rec["status"] == CF.CONFIRMED and not rec.get("_existing"):
                            flows = _flows(conn, env, td)
                            flow = _flow_since(flows, prev)
                except Exception as e:
                    log.warning(f"  P&L {env} {td}: cash-flow detection failed: {e}")
            day_pnl = _day_pnl(s, prev, flow)
            if flows and s["equity"] is not None:
                adj = flow_adjusted(conn, env, td, s["equity"])
                peak, dd = adj["peak"], adj["drawdown_pct"]
            else:
                prev_peak = prev["peak_equity"] if prev else None
                if flow and prev_peak is not None:
                    from portfolio.cash_flows import scale_peak
                    prev_peak = scale_peak(prev_peak, prev.get("equity"), flow)
                peak = max(x for x in (s["equity"], prev_peak) if x is not None) \
                    if (s["equity"] is not None or prev_peak is not None) else None
                dd = round((peak - s["equity"]) / peak * 100, 2) if peak and s["equity"] is not None else None
            conn.execute(
                "INSERT INTO pnl_daily (date,env,n_positions,positions_value,cost,unrealised,realised_cum,cash,"
                "equity,day_pnl,peak_equity,drawdown_pct,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(date,env) DO UPDATE SET n_positions=excluded.n_positions,"
                "positions_value=excluded.positions_value,cost=excluded.cost,unrealised=excluded.unrealised,"
                "realised_cum=excluded.realised_cum,cash=excluded.cash,equity=excluded.equity,"
                "day_pnl=excluded.day_pnl,peak_equity=excluded.peak_equity,drawdown_pct=excluded.drawdown_pct,"
                "recorded_at=excluded.recorded_at",
                (str(td), env, s["n_positions"], s["positions_value"], s["cost"], s["unrealised"],
                 s["realised_cum"], s["cash"], s["equity"], day_pnl, peak, dd, datetime.now()))
            stored[env] = {"equity": s["equity"], "day_pnl": day_pnl, "drawdown_pct": dd,
                           "unvalued": s["unvalued"]}
            if s["unvalued"]:
                log.warning(f"  P&L {env} {td}: no price for {', '.join(s['unvalued'])} — left out of value")
        conn.commit()
    finally:
        conn.close()
    for env, v in stored.items():
        log.info(f"  ✓ Daily P&L {env} {td}: equity {v['equity']}, day {v['day_pnl']}, drawdown {v['drawdown_pct']}%")
    log_job("daily_pnl", "SUCCESS", len(stored), run_date=td)
    return {"status": "SUCCESS", "rows": len(stored), "envs": stored}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="ATIP portfolio P&L (paper and live)")
    ap.add_argument("--record", action="store_true", help="store today's pnl_daily rows")
    args = ap.parse_args()
    if args.record:
        print(record_daily_pnl())
    else:
        c = get_connection()
        try:
            for e in ENVS:
                s = portfolio_summary(c, e)
                print(f"{e}: {s['n_positions']} positions, value {s['positions_value']}, "
                      f"unrealised {s['unrealised']}, realised {s['realised_cum']}, equity {s['equity']}")
        finally:
            c.close()
