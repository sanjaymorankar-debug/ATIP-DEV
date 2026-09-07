"""
ATIP — Append-only signal log and momentum outcome tracking
===========================================================
Two tables, deliberately separated:

  signal_log      IMMUTABLE. One row per signal actually emitted by a scoring
                  run. Never updated, never upserted. Re-running a date appends
                  a new row with a new run_id rather than replacing the old one.

  signal_outcome  DERIVED. Per (signal, threshold): did price reach it, when,
                  and how many sessions did it take. Recomputed as new price
                  data arrives, which is why it is separate from the log.

WHY THIS EXISTS
    ai_scores and predictions both use ON CONFLICT(symbol,date) DO UPDATE, so
    re-running a date REPLACES that day's signals. Observed live: after a
    Technical Score reweighting, 2026-07-30 silently went from BUY=6 to BUY=7.
    Those tables therefore answer "what would today's code say about that date",
    not "what did ATIP actually tell me at the time" — and the second question
    is the one a track record depends on. A tracker that grades against a record
    its own re-runs can rewrite is not evidence of anything.

    Every row is stamped with the git commit and a hash of the active weights,
    so a change in outcome can be attributed to a formula change rather than
    confused with market movement.

MOMENTUM
    Thresholds default to 3%, 6% and 8% and are configurable (atip_data/
    config.json -> momentum_thresholds, or the constant below). Direction
    follows the signal: a BUY is measured on the way up (high >= target), a
    SELL on the way down (low <= target), so "momentum" means "moved the way
    the signal said", both ways.

    Hits are detected on intraday high/low, not the close: a stock that touched
    +3% intraday and closed at +1% did reach the target — that is the exit a
    take-profit order would have taken. Same convention as scores/accuracy.py
    and scores/backtest.py, so all three agree.

USAGE
    python -m scores.signal_log --log --date 2026-09-07   # record today's signals
    python -m scores.signal_log --evaluate                # update outcomes
    python -m scores.signal_log --report                  # success rates
    python -m scores.signal_log --report --symbol RELIANCE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import uuid
from datetime import date, datetime
from pathlib import Path

from db.schema import get_connection, log_job

log = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = (3.0, 6.0, 8.0)
MAX_TRACK_SESSIONS = 30      # stop chasing a signal after this many sessions
CONFIG_PATH = Path("atip_data/config.json")

_VERSION_CACHE = {}


def momentum_thresholds():
    """Configurable via config.json -> momentum_thresholds; falls back to 3/6/8."""
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text())
            vals = cfg.get("momentum_thresholds")
            if isinstance(vals, list) and vals:
                out = sorted({round(float(v), 2) for v in vals if float(v) > 0})
                if out:
                    return tuple(out)
    except Exception as e:
        log.debug(f"  momentum_thresholds from config unavailable: {e}")
    return DEFAULT_THRESHOLDS


def sessions_between(d1, d2):
    """
    Trading sessions from d1 (exclusive) to d2 (inclusive), per the NSE
    calendar.

    Timing MUST come from the calendar, not from counting available price bars.
    Counting bars silently reports "hit in 1 session" when the only forward bar
    is five weeks later because the pipeline wasn't run in between — which is
    exactly what the first version of this did, turning a 27-session move into
    a headline "hit 8% in 1 day".
    """
    try:
        from utils.trading_calendar import is_trading_day
        from datetime import timedelta
        a = date.fromisoformat(str(d1)); b = date.fromisoformat(str(d2))
        if b <= a:
            return 0
        n, cur = 0, a
        while cur < b and n < 400:
            cur += timedelta(days=1)
            if is_trading_day(cur):
                n += 1
        return n
    except Exception:
        return None


def _git_commit():
    if "git" not in _VERSION_CACHE:
        try:
            _VERSION_CACHE["git"] = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                text=True, timeout=10).stdout.strip() or "unknown"
        except Exception:
            _VERSION_CACHE["git"] = "unknown"
    return _VERSION_CACHE["git"]


def _weights_hash(conn):
    """Short hash of the active weight set, so a formula change is visible."""
    rows = conn.execute("SELECT index_name,variable,weight FROM weight_config "
                        "WHERE active=1 ORDER BY index_name,variable").fetchall()
    blob = ";".join(f"{r[0]}.{r[1]}={r[2]}" for r in rows)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════════════════
#  SCHEMA
# ═══════════════════════════════════════════════════════════════════════════

def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id             TEXT PRIMARY KEY,
            run_id         TEXT NOT NULL,
            logged_at      TEXT NOT NULL,
            signal_date    DATE NOT NULL,
            symbol         TEXT NOT NULL,
            signal         TEXT NOT NULL,
            entry_price    REAL,
            atip_score REAL, vpi REAL, spi REAL, rri REAL, mri REAL,
            cri REAL, msi REAL, zpi REAL, acs REAL,
            mh_score REAL, regime TEXT, is_tod INTEGER DEFAULT 0,
            model_version  TEXT,
            weights_hash   TEXT,
            notes          TEXT
        )
    """)
    # No UNIQUE on (signal_date,symbol) — that is the point. Multiple runs of the
    # same date coexist, distinguished by run_id, so history is never rewritten.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_siglog_date ON signal_log(signal_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_siglog_symbol ON signal_log(symbol,signal_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_siglog_run ON signal_log(run_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcome (
            signal_id      TEXT NOT NULL,
            threshold_pct  REAL NOT NULL,
            hit            INTEGER DEFAULT 0,
            hit_date       DATE,
            sessions_to_hit INTEGER,
            hit_price      REAL,
            max_favourable_pct REAL,
            max_adverse_pct    REAL,
            sessions_tracked   INTEGER DEFAULT 0,
            still_open     INTEGER DEFAULT 1,
            data_gap_sessions  INTEGER DEFAULT 0,
            evaluated_at   TEXT,
            PRIMARY KEY (signal_id, threshold_pct)
        )
    """)
    # Additive migration for databases created before data_gap_sessions existed.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signal_outcome)").fetchall()}
        if "data_gap_sessions" not in cols:
            conn.execute("ALTER TABLE signal_outcome ADD COLUMN data_gap_sessions INTEGER DEFAULT 0")
    except Exception as e:
        log.warning(f"  signal_outcome migration skipped: {e}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sigout_hit ON signal_outcome(threshold_pct,hit)")
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
#  LOGGING (append-only)
# ═══════════════════════════════════════════════════════════════════════════

def log_signals(trade_date=None, conn=None, actionable_only=True) -> dict:
    """
    Append the signals produced for trade_date.

    actionable_only=True records BUY/SELL only — a HOLD/WAIT on 480 stocks is
    not a recommendation anyone acts on, and logging all 501 every run would
    bury the signals that matter. Pass False to record everything.
    """
    own = conn is None
    if own:
        conn = get_connection()
    if trade_date is None:
        trade_date = date.today()
    result = {"date": str(trade_date), "logged": 0, "status": "SUCCESS"}
    try:
        ensure_tables(conn)
        where = "AND s.signal IN ('BUY','SELL')" if actionable_only else ""
        rows = conn.execute(f"""
            SELECT s.*, p.close AS entry_price
            FROM ai_scores s
            LEFT JOIN prices_daily p ON p.symbol=s.symbol AND p.date=s.date
            WHERE s.date=? {where}
        """, (str(trade_date),)).fetchall()

        run_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        ver, wh = _git_commit(), _weights_hash(conn)

        for r in rows:
            d = dict(r)
            conn.execute("""
                INSERT INTO signal_log (id, run_id, logged_at, signal_date, symbol, signal,
                    entry_price, atip_score, vpi, spi, rri, mri, cri, msi, zpi, acs,
                    mh_score, regime, is_tod, model_version, weights_hash, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (str(uuid.uuid4()), run_id, now, str(trade_date), d["symbol"], d["signal"],
                  d.get("entry_price"), d.get("atip_score"), d.get("vpi"), d.get("spi"),
                  d.get("rri"), d.get("mri"), d.get("cri"), d.get("msi"), d.get("zpi"),
                  d.get("acs"), d.get("mh_score"), d.get("regime"), d.get("is_tod") or 0,
                  ver, wh, d.get("top_factor_1")))
            result["logged"] += 1

        conn.commit()
        log.info(f"  ✓ Signal log: {result['logged']} signals appended "
                 f"(run {run_id}, model {ver}, weights {wh})")
        log_job("signal_log", "SUCCESS", result["logged"], run_date=trade_date)
    except Exception as e:
        conn.rollback()
        result["status"] = "FAILED"
        log.error(f"  ✗ Signal log failed: {e}")
        log_job("signal_log", "FAILED", 0, error=e, run_date=trade_date)
    finally:
        if own:
            conn.close()
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  OUTCOME EVALUATION
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_outcomes(max_sessions=MAX_TRACK_SESSIONS) -> dict:
    """
    Walk forward from each signal and record, per threshold, whether price moved
    the signal's way by that much and how many sessions it took.

    Idempotent and resumable: a signal already resolved for a threshold is
    skipped; one still open is re-checked as new bars arrive.
    """
    conn = get_connection()
    res = {"evaluated": 0, "resolved": 0, "status": "SUCCESS"}
    try:
        ensure_tables(conn)
        ths = momentum_thresholds()
        sigs = conn.execute("""
            SELECT id, symbol, signal, signal_date, entry_price
            FROM signal_log
            WHERE entry_price IS NOT NULL AND signal IN ('BUY','SELL')
            ORDER BY signal_date
        """).fetchall()

        now = datetime.now().isoformat()
        for s in sigs:
            done = {r["threshold_pct"] for r in conn.execute(
                "SELECT threshold_pct FROM signal_outcome WHERE signal_id=? AND still_open=0",
                (s["id"],)).fetchall()}
            todo = [t for t in ths if t not in done]
            if not todo:
                continue

            bars = conn.execute("""
                SELECT date, high, low, close FROM prices_daily
                WHERE symbol=? AND date>? ORDER BY date LIMIT ?
            """, (s["symbol"], str(s["signal_date"]), max_sessions)).fetchall()
            if not bars:
                continue

            entry = s["entry_price"]
            long_side = s["signal"] == "BUY"

            # Best/worst excursion, in the signal's own direction.
            mfe = mae = 0.0
            for b in bars:
                up = (b["high"] - entry) / entry * 100
                dn = (b["low"] - entry) / entry * 100
                fav, adv = (up, dn) if long_side else (-dn, -up)
                mfe = max(mfe, fav)
                mae = min(mae, adv)

            # Sessions between the signal and its first forward bar. Anything
            # above 1 means the pipeline didn't run in between, so this signal's
            # TIMING is unmeasurable even though its hit/miss is still valid.
            gap = sessions_between(s["signal_date"], bars[0]["date"]) or 0
            gap = max(0, gap - 1)

            for t in todo:
                target = entry * (1 + t / 100) if long_side else entry * (1 - t / 100)
                hit, hit_date, hit_price, sess = 0, None, None, None
                for b in bars:
                    touched = (b["high"] >= target) if long_side else (b["low"] <= target)
                    if touched:
                        hit, hit_date, hit_price = 1, str(b["date"]), round(target, 2)
                        # Calendar sessions, not bar count — see sessions_between().
                        sess = sessions_between(s["signal_date"], b["date"])
                        break
                # Open until it hits or the tracking window is exhausted.
                still_open = 0 if (hit or len(bars) >= max_sessions) else 1
                conn.execute("""
                    INSERT INTO signal_outcome (signal_id, threshold_pct, hit, hit_date,
                        sessions_to_hit, hit_price, max_favourable_pct, max_adverse_pct,
                        sessions_tracked, still_open, data_gap_sessions, evaluated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(signal_id,threshold_pct) DO UPDATE SET
                        hit=excluded.hit, hit_date=excluded.hit_date,
                        sessions_to_hit=excluded.sessions_to_hit, hit_price=excluded.hit_price,
                        max_favourable_pct=excluded.max_favourable_pct,
                        max_adverse_pct=excluded.max_adverse_pct,
                        sessions_tracked=excluded.sessions_tracked,
                        still_open=excluded.still_open,
                        data_gap_sessions=excluded.data_gap_sessions,
                        evaluated_at=excluded.evaluated_at
                """, (s["id"], t, hit, hit_date, sess, hit_price,
                      round(mfe, 2), round(mae, 2), len(bars), still_open, gap, now))
                res["evaluated"] += 1
                if not still_open:
                    res["resolved"] += 1

        conn.commit()
        log.info(f"  ✓ Signal outcomes: {res['evaluated']} evaluated, {res['resolved']} resolved")
        log_job("signal_outcomes", "SUCCESS", res["evaluated"])
    except Exception as e:
        conn.rollback()
        res["status"] = "FAILED"
        log.error(f"  ✗ Outcome evaluation failed: {e}")
        log_job("signal_outcomes", "FAILED", 0, error=e)
    finally:
        conn.close()
    return res


# ═══════════════════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════════════════

def success_report(symbol=None, since=None, signal=None) -> dict:
    """
    Hit rate per (signal, threshold) with median sessions-to-hit.

    Rates are computed over RESOLVED signals only — one still inside its
    tracking window is neither a hit nor a miss yet, and counting it as a miss
    would understate the rate on recent signals.
    """
    conn = get_connection()
    try:
        ensure_tables(conn)
        where, params = ["l.entry_price IS NOT NULL"], []
        if symbol:
            where.append("l.symbol=?"); params.append(symbol.upper())
        if since:
            where.append("l.signal_date>=?"); params.append(str(since))
        if signal:
            where.append("l.signal=?"); params.append(signal.upper())
        w = " AND ".join(where)

        rows = conn.execute(f"""
            SELECT l.signal, o.threshold_pct, o.hit, o.sessions_to_hit, o.still_open,
                   o.max_favourable_pct, o.max_adverse_pct,
                   COALESCE(o.data_gap_sessions,0) AS gap
            FROM signal_log l JOIN signal_outcome o ON o.signal_id=l.id
            WHERE {w}
        """, params).fetchall()

        buckets = {}
        for r in rows:
            k = (r["signal"], r["threshold_pct"])
            b = buckets.setdefault(k, {"resolved": 0, "hits": 0, "open": 0,
                                       "days": [], "mfe": [], "mae": [], "gapped": 0})
            if r["still_open"]:
                b["open"] += 1
            else:
                b["resolved"] += 1
                if r["hit"]:
                    b["hits"] += 1
                    # Timing only counts when the forward data is contiguous. A
                    # signal whose next bar is weeks later did reach the target,
                    # but "how long it took" is unknowable — including it would
                    # report a 27-session move as 1 session.
                    if r["sessions_to_hit"] and not r["gap"]:
                        b["days"].append(r["sessions_to_hit"])
                    elif r["gap"]:
                        b["gapped"] += 1
            if r["max_favourable_pct"] is not None:
                b["mfe"].append(r["max_favourable_pct"]); b["mae"].append(r["max_adverse_pct"])

        import statistics
        out = []
        for (sig, th), b in sorted(buckets.items()):
            out.append({
                "signal": sig, "threshold_pct": th,
                "resolved": b["resolved"], "hits": b["hits"], "open": b["open"],
                "hit_rate": round(b["hits"] / b["resolved"] * 100, 1) if b["resolved"] else None,
                "median_sessions": round(statistics.median(b["days"]), 1) if b["days"] else None,
                "fastest_sessions": min(b["days"]) if b["days"] else None,
                "slowest_sessions": max(b["days"]) if b["days"] else None,
                "avg_mfe": round(sum(b["mfe"]) / len(b["mfe"]), 2) if b["mfe"] else None,
                "avg_mae": round(sum(b["mae"]) / len(b["mae"]), 2) if b["mae"] else None,
                "timing_unmeasurable": b["gapped"],
            })
        totals = conn.execute(f"""
            SELECT COUNT(*) n, COUNT(DISTINCT l.signal_date) days,
                   COUNT(DISTINCT l.symbol) syms, MIN(l.signal_date) first, MAX(l.signal_date) last
            FROM signal_log l WHERE {w}
        """, params).fetchone()
        return {"buckets": out, "total_signals": totals["n"], "dates": totals["days"],
                "symbols": totals["syms"], "first": str(totals["first"] or ""),
                "last": str(totals["last"] or ""), "thresholds": list(momentum_thresholds())}
    finally:
        conn.close()


def recent_signals(limit=200, symbol=None):
    """Signal history with per-threshold outcome, newest first — for the UI."""
    conn = get_connection()
    try:
        ensure_tables(conn)
        w, p = "", []
        if symbol:
            w = "WHERE l.symbol=?"; p.append(symbol.upper())
        rows = conn.execute(f"""
            SELECT l.id, l.signal_date, l.symbol, l.signal, l.entry_price, l.atip_score,
                   l.zpi, l.cri, l.acs, l.regime, l.is_tod, l.model_version, l.logged_at
            FROM signal_log l {w}
            ORDER BY l.signal_date DESC, l.atip_score DESC LIMIT ?
        """, p + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["outcomes"] = [dict(o) for o in conn.execute(
                "SELECT threshold_pct,hit,sessions_to_hit,hit_date,still_open,"
                "max_favourable_pct,max_adverse_pct FROM signal_outcome "
                "WHERE signal_id=? ORDER BY threshold_pct", (r["id"],)).fetchall()]
            out.append(d)
        return out
    finally:
        conn.close()


def print_report(symbol=None, since=None):
    rep = success_report(symbol=symbol, since=since)
    print(f"\n{'='*74}")
    print(f"  ATIP Signal History — {symbol or 'ALL SYMBOLS'}")
    print(f"  {rep['total_signals']} signals · {rep['dates']} dates · {rep['symbols']} symbols"
          + (f" · {rep['first']} to {rep['last']}" if rep['first'] else ""))
    print(f"  Momentum thresholds: {', '.join(str(t)+'%' for t in rep['thresholds'])}")
    print(f"{'='*74}")
    if not rep["buckets"]:
        print("\n  No evaluated signals yet. Run --log then --evaluate.\n")
        return
    print(f"\n  {'Signal':7} {'Target':>7} {'Resolved':>9} {'Hits':>6} {'Hit rate':>9} "
          f"{'Median':>8} {'Fastest':>8} {'Slowest':>8} {'Open':>5}")
    print("  " + "-" * 72)
    for b in rep["buckets"]:
        hr = f"{b['hit_rate']}%" if b["hit_rate"] is not None else "—"
        md = f"{b['median_sessions']}d" if b["median_sessions"] else "n/a"
        fa = f"{b['fastest_sessions']}d" if b["fastest_sessions"] else "—"
        sl = f"{b['slowest_sessions']}d" if b["slowest_sessions"] else "—"
        print(f"  {b['signal']:7} {b['threshold_pct']:>6}% {b['resolved']:>9} {b['hits']:>6} "
              f"{hr:>9} {md:>8} {fa:>8} {sl:>8} {b['open']:>5}")
    gapped = sum(b.get("timing_unmeasurable") or 0 for b in rep["buckets"])
    thin = [b for b in rep["buckets"] if b["resolved"] and b["resolved"] < 20]
    print(f"\n  Median/fastest/slowest are NSE trading SESSIONS from signal to first touch.")
    print(f"  Hit rates exclude signals still inside the {MAX_TRACK_SESSIONS}-session window.")
    if gapped:
        print(f"  {gapped} hit(s) excluded from timing: the price history has a gap after the")
        print(f"  signal (pipeline not run in between), so the target was reached but WHEN is")
        print(f"  unknowable. Hit/miss still counts; only the elapsed time is discarded.")
    if thin:
        bits = ", ".join("{}@{}%={}".format(b["signal"], b["threshold_pct"], b["resolved"])
                         for b in thin)
        print(f"  ⚠ Small sample: {bits}")
        print(f"    resolved signals. Treat these percentages as indicative, not a track record.")
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP append-only signal log and momentum tracking")
    ap.add_argument("--log", action="store_true", help="append signals for --date")
    ap.add_argument("--all-signals", action="store_true", help="with --log: record HOLD/WAIT too")
    ap.add_argument("--evaluate", action="store_true", help="update momentum outcomes")
    ap.add_argument("--report", action="store_true", help="print success rates")
    ap.add_argument("--backfill", action="store_true", help="log every scored date that has none")
    ap.add_argument("--date")
    ap.add_argument("--symbol")
    ap.add_argument("--since")
    args = ap.parse_args()

    if args.backfill:
        c = get_connection()
        try:
            ensure_tables(c)
            dates = [r[0] for r in c.execute("""
                SELECT DISTINCT s.date FROM ai_scores s
                WHERE NOT EXISTS (SELECT 1 FROM signal_log l WHERE l.signal_date=s.date)
                ORDER BY s.date""").fetchall()]
        finally:
            c.close()
        total = sum(log_signals(d, actionable_only=not args.all_signals).get("logged", 0)
                    for d in dates)
        print(f"backfilled {total} signals across {len(dates)} dates")
        evaluate_outcomes()
        print_report()
    elif args.log:
        td = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
        print(log_signals(td, actionable_only=not args.all_signals))
    elif args.evaluate:
        print(evaluate_outcomes())
    else:
        print_report(symbol=args.symbol, since=args.since)
