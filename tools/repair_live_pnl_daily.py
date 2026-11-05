"""
One-off repair (profit audit 5 Nov 2026, PA-01): take the paper account's cash out of the LIVE book's pnl_daily rows.

Until the fix, portfolio/pnl.py _live_cash() asked orders.broker.available_balance(), which asks the broker that
would receive an order -- under broker_env PAPER, the simulator. So every LIVE row from 25 Sep 2026 stored the PAPER
account's balance as the real account's cash (Rs 5.05 lakh, then Rs 3.17 lakh on 5 Nov, while the real Dhan account
held Rs 2,350.16). Equity, day P&L, the peak and the drawdown of those rows all carry it: the paper sessions' buys of
4 Nov read as a LIVE day P&L of -Rs 1,43,596.19 and a 25 % drawdown, and the stored peak (Rs 5,83,357.74) would read
as an ~87 % drawdown -- every BUY refused by max_drawdown_pct -- the day broker_env turns LIVE.

What the real account's cash was on those days is not recorded anywhere, so the repair does not invent it: each
affected row keeps its holdings figures (positions value, cost, unrealised) and loses cash, equity, peak and
drawdown (NULL = unknown); its day P&L becomes the change in unrealised + realised against the previous LIVE row --
the basis portfolio/pnl.py already uses when cash is unknown. From the first row recorded after the fix, cash is the
real account's and the peak starts there.

Which rows: env = 'LIVE' with a cash figure, dated on or before --through (default 2026-11-05, the last row written
before the fix). Safe to run twice: a repaired row has no cash and no longer qualifies.

    python -m tools.repair_live_pnl_daily              # dry run: every change, nothing written
    python -m tools.repair_live_pnl_daily --apply      # after `python -m ops backup --kind pre-release`

Arithmetic in Python over a SELECT, so it runs on SQLite, MySQL or PostgreSQL alike.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.schema import get_connection  # noqa: E402


def plan(conn, through: str) -> list:
    """[(row, changes)] for the LIVE rows that carry a cash figure, oldest first."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM pnl_daily WHERE env = 'LIVE' ORDER BY date").fetchall()]
    out, prev = [], None
    for r in rows:
        if r["cash"] is not None and str(r["date"])[:10] <= through:
            basis = (r["unrealised"] or 0) + (r["realised_cum"] or 0)
            day = round(basis - ((prev["unrealised"] or 0) + (prev["realised_cum"] or 0)), 2) if prev else None
            out.append((r, {"cash": None, "equity": None, "peak_equity": None, "drawdown_pct": None, "day_pnl": day}))
        prev = r
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Take the paper cash out of the LIVE book's pnl_daily rows (PA-01)")
    ap.add_argument("--through", default="2026-11-05", help="last date written before the fix (YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args(argv)
    conn = get_connection()
    try:
        todo = plan(conn, a.through)
        print(f"{len(todo)} LIVE row(s) carry a cash figure on or before {a.through}"
              + ("" if a.apply else " -- dry run, nothing written"))
        print(f"  {'date':<10} {'cash':>12} {'equity':>12} {'day P&L':>12} -> {'day P&L':>10}  drawdown")
        for r, ch in todo:
            print(f"  {str(r['date'])[:10]:<10} {r['cash'] or 0:>12,.2f} {r['equity'] or 0:>12,.2f} {r['day_pnl'] or 0:>12,.2f}"
                  f" -> {ch['day_pnl'] if ch['day_pnl'] is not None else '-':>10}  {r['drawdown_pct']}% -> unknown")
        if a.apply and todo:
            for r, ch in todo:
                conn.execute("UPDATE pnl_daily SET cash = ?, equity = ?, peak_equity = ?, drawdown_pct = ?, day_pnl = ? "
                             "WHERE id = ?", (ch["cash"], ch["equity"], ch["peak_equity"], ch["drawdown_pct"],
                                              ch["day_pnl"], r["id"]))
            conn.commit()
            print(f"  repaired {len(todo)} row(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
