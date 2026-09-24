"""
Data quality layer (DP-19): is a session's stored data right, not just present?

The pipeline already refuses to score a session that is mostly missing
(_eod_coverage) or a byte copy of the previous one (_eod_freshness), and the
index/benchmark code repairs what it can. Nothing recorded, per session, what
was actually wrong -- which is how 280 bars with close outside [low, high],
304 unexplained 25% moves and 8,858 copied bars sat in prices_daily for months.

run_data_quality(td) runs every check below on session td over the tracked
universe (Nifty 500 + holdings), stores one data_quality row per check and a
DataQualityScore row (check_name 'DQS'), and raises an alert (dashboard +
Telegram) when the session is not clean.

  check                 what counts as failed
  missing_bars          tracked symbol with no bar for the session
  invalid_values        close/open/high/low missing or <= 0, volume < 0
  ohlc_relationship     high < low, or open/close outside [low, high]
  stale_bars            bar identical (OHLC + volume) to the symbol's previous bar
  abnormal_moves        |close / previous close - 1| > 20% with no corporate action that day
  gaps                  bar present today, absent the previous session, present before it
  unknown_symbols       tracked symbol missing from Dhan's security list
  index_series          a daily index series (NIFTY50, INDIAVIX, ...) without the session's close
  index_levels_hours    index_levels rows for the session outside 09:00-15:45
  index_levels_dupes    index_levels rows sharing (date, time)
  non_session_dates     prices_daily rows dated on a non-trading day in the last 30 days
  future_dates          prices_daily / index_levels rows dated after today
  market_inputs         the session has no FII/DII row (info only: NSE publishes it late)

    python -m data.quality                 # the last session
    python -m data.quality --date 2026-09-23
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta

from db.schema import get_connection, log_job
from utils.trading_calendar import is_trading_day, last_trading_day

log = logging.getLogger(__name__)

ABNORMAL_MOVE = 0.20
# check -> (weight in the DQS, failure share at which it is a WARN, an ERROR)
CHECKS = {
    "missing_bars":       (0.20, 0.02, 0.20),
    "invalid_values":     (0.15, 0.0001, 0.01),
    "ohlc_relationship":  (0.15, 0.0001, 0.01),
    "stale_bars":         (0.15, 0.01, 0.05),
    "abnormal_moves":     (0.05, 0.005, 0.03),
    "gaps":               (0.05, 0.01, 0.05),
    "unknown_symbols":    (0.05, 0.01, 0.05),
    "index_series":       (0.10, 0.0001, 0.5),
    "index_levels_hours": (0.03, 0.0001, 0.05),
    "index_levels_dupes": (0.02, 0.0001, 0.01),
    "non_session_dates":  (0.03, 0.0001, 0.01),
    "future_dates":       (0.02, 0.0001, 0.0001),
}
DQS_ALERT_BELOW = 90.0


def _previous_session(td: date) -> date:
    return last_trading_day(td - timedelta(days=1))


def _in(universe):
    return ",".join("?" * len(universe))


def _result(name, failed, checked, sample=None, note=None):
    return {"check": name, "failed": int(failed or 0), "checked": int(checked or 0),
            "sample": list(sample or [])[:10], "note": note}


def run_checks(conn, td: date, universe: list[str]) -> list[dict]:
    """Every check for session td; pure reads."""
    d, prev = str(td), str(_previous_session(td))
    U = sorted(set(universe))
    out = []

    bars = {r[0]: r for r in conn.execute(
        f"SELECT symbol, open, high, low, close, volume FROM prices_daily WHERE date=? AND symbol IN ({_in(U)})",
        (d, *U))} if U else {}
    prev_bars = {r[0]: r for r in conn.execute(
        f"SELECT symbol, open, high, low, close, volume FROM prices_daily WHERE date=? AND symbol IN ({_in(U)})",
        (prev, *U))} if U else {}

    missing = [s for s in U if s not in bars]
    out.append(_result("missing_bars", len(missing), len(U), missing))

    def bad_value(r):
        _, o, h, l, c, v = r
        return any(x is None or x <= 0 for x in (o, h, l, c)) or (v is not None and v < 0)
    invalid = [s for s, r in bars.items() if bad_value(r)]
    out.append(_result("invalid_values", len(invalid), len(bars), invalid))

    tol = 1e-4
    def bad_ohlc(r):
        _, o, h, l, c, _ = r
        if None in (o, h, l, c):
            return False                       # counted under invalid_values
        return h < l * (1 - tol) or not (l * (1 - tol) <= c <= h * (1 + tol)) \
            or not (l * (1 - tol) <= o <= h * (1 + tol))
    ohlc = [s for s, r in bars.items() if bad_ohlc(r)]
    out.append(_result("ohlc_relationship", len(ohlc), len(bars), ohlc))

    both = [s for s in bars if s in prev_bars]
    stale = [s for s in both if tuple(bars[s][1:]) == tuple(prev_bars[s][1:])]
    out.append(_result("stale_bars", len(stale), len(both), stale))

    ca = {r[0] for r in conn.execute("SELECT symbol FROM corporate_actions WHERE ex_date=?", (d,))} \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='corporate_actions'").fetchone() else set()
    moves = []
    for s in both:
        c, pc = bars[s][4], prev_bars[s][4]
        if c and pc and abs(c / pc - 1) > ABNORMAL_MOVE and s not in ca:
            moves.append(f"{s} {(c / pc - 1) * 100:+.1f}%")
    out.append(_result("abnormal_moves", len(moves), len(both), moves))

    older = {r[0] for r in conn.execute(
        f"SELECT DISTINCT symbol FROM prices_daily WHERE date<? AND date>=? AND symbol IN ({_in(U)})",
        (prev, str(td - timedelta(days=30)), *U))} if U else set()
    gaps = [s for s in bars if s not in prev_bars and s in older]
    out.append(_result("gaps", len(gaps), len(bars), gaps))

    try:
        from data.dhan import SEC_LIST_PATH, load_security_map
        if SEC_LIST_PATH.exists():                  # never download from a quality check
            sec = load_security_map()
            unknown = [s for s in U if s not in sec]
            out.append(_result("unknown_symbols", len(unknown), len(U), unknown))
        else:
            out.append(_result("unknown_symbols", 0, 0, note="security list not downloaded"))
    except Exception as e:
        out.append(_result("unknown_symbols", 0, 0, note=f"not checked: {e}"))

    from data.dhan import INDEX_SERIES_SYMBOLS
    series = list(INDEX_SERIES_SYMBOLS.values())
    have = {r[0] for r in conn.execute(
        f"SELECT symbol FROM prices_daily WHERE date=? AND symbol IN ({_in(series)})", (d, *series))}
    out.append(_result("index_series", len(set(series) - have), len(series), sorted(set(series) - have)))

    n_idx, n_out = conn.execute(
        "SELECT COUNT(*), SUM(time < '09:00:00' OR time > '15:45:59') FROM index_levels WHERE date=?", (d,)
    ).fetchone()
    out.append(_result("index_levels_hours", n_out, n_idx))
    dupes = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM index_levels WHERE date=? "
                         "GROUP BY date, time HAVING COUNT(*) > 1)", (d,)).fetchone()[0]
    out.append(_result("index_levels_dupes", dupes, n_idx))

    recent = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT date FROM prices_daily WHERE date>=? AND date<=?", (str(td - timedelta(days=30)), d))]
    non_session = [x for x in recent if not is_trading_day(date.fromisoformat(x[:10]))]
    out.append(_result("non_session_dates", len(non_session), len(recent), non_session))

    today = str(date.today())
    fut = conn.execute("SELECT (SELECT COUNT(*) FROM prices_daily WHERE date>?) + "
                       "(SELECT COUNT(*) FROM index_levels WHERE date>?)", (today, today)).fetchone()[0]
    out.append(_result("future_dates", fut, max(fut, 1)))

    fii = conn.execute("SELECT 1 FROM fii_dii_market WHERE date=?", (d,)).fetchone()
    out.append(_result("market_inputs", 0 if fii else 1, 1,
                       note=None if fii else "no FII/DII row for the session yet (NSE publishes it in the evening)"))
    return out


def score(results: list[dict]) -> tuple[float, list[dict]]:
    """(DQS 0-100, results with pass_rate and severity). The DQS is the
    weighted pass rate of the weighted checks; a check with nothing to check
    is left out of it."""
    tot_w = tot = 0.0
    for r in results:
        rate = 1.0 - (r["failed"] / r["checked"]) if r["checked"] else None
        r["pass_rate"] = round(rate * 100, 2) if rate is not None else None
        w, warn, err = CHECKS.get(r["check"], (0.0, None, None))
        share = (r["failed"] / r["checked"]) if r["checked"] else 0.0
        r["severity"] = ("INFO" if r["check"] not in CHECKS else
                         "ERROR" if err is not None and share >= err and r["failed"] else
                         "WARN" if warn is not None and share >= warn and r["failed"] else "OK")
        if r["check"] == "market_inputs" and r["failed"]:
            r["severity"] = "INFO"
        if rate is not None and w:
            tot_w += w
            tot += w * rate
    return (round(tot / tot_w * 100, 2) if tot_w else 0.0), results


def run_data_quality(trade_date=None) -> dict:
    """Check session trade_date (default: the last session), store the results
    and alert on a session that is not clean. run_job compatible."""
    td = date.fromisoformat(str(trade_date)[:10]) if trade_date else last_trading_day(date.today())
    if not is_trading_day(td):
        return {"status": "SKIPPED", "rows": 0, "reason": f"{td} is not a session"}
    conn = get_connection()
    try:
        from data.dhan import get_tracked_symbols
        universe = list(get_tracked_symbols(conn))
        dqs, results = score(run_checks(conn, td, universe))
        now = datetime.now()
        rows = [(str(td), r["check"], r["severity"], r["failed"], r["checked"], r["pass_rate"],
                 json.dumps({"sample": r["sample"], "note": r["note"]}), now) for r in results]
        worst = ("ERROR" if any(r["severity"] == "ERROR" for r in results) else
                 "WARN" if any(r["severity"] == "WARN" for r in results) else "OK")
        rows.append((str(td), "DQS", worst, sum(r["failed"] for r in results if r["check"] in CHECKS),
                     sum(r["checked"] for r in results if r["check"] in CHECKS), dqs,
                     json.dumps({"universe": len(universe)}), now))
        conn.executemany(
            "INSERT INTO data_quality (date,check_name,severity,failed,checked,score,detail,run_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(date,check_name) DO UPDATE SET severity=excluded.severity,"
            "failed=excluded.failed,checked=excluded.checked,score=excluded.score,detail=excluded.detail,"
            "run_at=excluded.run_at", rows)
        conn.commit()
    finally:
        conn.close()
    problems = [r for r in results if r["severity"] in ("WARN", "ERROR")]
    log.info(f"  ✓ Data quality {td}: DQS {dqs:.1f} ({worst})"
             + (" — " + "; ".join(f"{r['check']} {r['failed']}/{r['checked']}" for r in problems) if problems else ""))
    if worst == "ERROR" or dqs < DQS_ALERT_BELOW:
        try:
            from alerts.telegram import notify, fmt, _html
            body = "\n".join(f"<b>{r['severity']}</b> {r['check']}: {r['failed']}/{r['checked']}"
                             + (f" — e.g. {_html(', '.join(map(str, r['sample'][:5])))}" if r["sample"] else "")
                             for r in problems)
            notify(fmt("🧪", f"Data Quality {td}: DQS {dqs:.0f}", body or "below threshold"),
                   category="data_quality", severity="error" if worst == "ERROR" else "warning",
                   key=f"dq:{td}")
        except Exception as e:
            log.warning(f"  data-quality alert not raised: {e}")
    log_job("data_quality", worst, len(results), run_date=td)
    return {"status": "SUCCESS", "rows": len(results), "dqs": dqs, "worst": worst,
            "problems": {r["check"]: r["failed"] for r in problems}}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="ATIP data-quality checks for one session")
    ap.add_argument("--date", help="session, YYYY-MM-DD (default: the last session)")
    print(run_data_quality(ap.parse_args().date))
