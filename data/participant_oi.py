"""
W39 (MP-04) — NSE participant-wise open interest: how Clients, DIIs, FIIs and Pros are
positioned in index / stock futures and options at each day's close.

Source: NSE's daily file (published in the evening, after the F&O close)
    https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv
    (archives.nseindia.com is tried second). A title line, then a header row:
    Client Type, Future Index Long, Future Index Short, Future Stock Long, Future Stock Short,
    Option Index Call Long, Option Index Put Long, Option Index Call Short, Option Index Put Short,
    Option Stock Call Long, Option Stock Put Long, Option Stock Call Short, Option Stock Put Short,
    Total Long Contracts, Total Short Contracts   (headers carry stray tabs / spaces: normalised)

Table fo_participant_oi (date, client_type) with every column above, in contracts.
research/market_pulse.py derives the FII index-futures long % and its changes from it.
A missing file (404: holiday, or not yet published) is skipped, not an error.

CLI: python -m data.participant_oi [--days 30]      fetch the last N calendar days (idempotent)
Scheduled daily at 20:15 on market days (pipeline/scheduler.py).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

URLS = ("https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv",
        "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv")

COLUMNS = {
    "future index long": "fut_idx_long", "future index short": "fut_idx_short",
    "future stock long": "fut_stk_long", "future stock short": "fut_stk_short",
    "option index call long": "opt_idx_call_long", "option index put long": "opt_idx_put_long",
    "option index call short": "opt_idx_call_short", "option index put short": "opt_idx_put_short",
    "option stock call long": "opt_stk_call_long", "option stock put long": "opt_stk_put_long",
    "option stock call short": "opt_stk_call_short", "option stock put short": "opt_stk_put_short",
    "total long contracts": "total_long", "total short contracts": "total_short",
}
CLIENT_TYPES = ("Client", "DII", "FII", "Pro", "TOTAL")

DDL = ("""CREATE TABLE IF NOT EXISTS fo_participant_oi (
    date DATE NOT NULL, client_type TEXT NOT NULL, """ + ", ".join(f"{c} REAL" for c in COLUMNS.values()) +
       """, fetched_at TIMESTAMP, PRIMARY KEY (date, client_type))""",)


def ensure_tables(conn):
    for d in DDL:
        conn.execute(d)


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").replace("\t", " ")).strip().lower()


def parse(text: str) -> list:
    """Rows [{client_type, <columns>}] from the CSV text; [] when it is not the expected file."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    hdr_i = next((i for i, ln in enumerate(lines) if _norm(ln).startswith("client type")), None)
    if hdr_i is None:
        return []
    reader = csv.reader(io.StringIO("\n".join(lines[hdr_i:])))
    header = [_norm(h) for h in next(reader)]
    out = []
    for row in reader:
        if not row or not row[0].strip():
            continue
        rec = {"client_type": row[0].strip()}
        for h, v in zip(header[1:], row[1:]):
            col = COLUMNS.get(h)
            if col:
                try:
                    rec[col] = float(str(v).replace(",", "").strip())
                except ValueError:
                    rec[col] = None
        if rec["client_type"] in CLIENT_TYPES:
            out.append(rec)
    return out


def fetch_day(d: date, nse=None) -> list | None:
    """Parsed rows for one day, [] if NSE has no file for it, None if NSE could not be reached."""
    from data.nse_api import client
    nse = nse or client()
    tag = d.strftime("%d%m%Y")
    reached = False
    for url in URLS:
        text = nse.text(url.format(d=tag))
        if text is None:
            continue
        reached = True
        rows = parse(text)
        if rows:
            return rows
    return [] if reached else None


def store(conn, d: date, rows: list) -> int:
    ensure_tables(conn)
    cols = list(COLUMNS.values())
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        conn.execute(f"INSERT INTO fo_participant_oi (date, client_type, {', '.join(cols)}, fetched_at) VALUES "
                     f"({','.join('?' * (len(cols) + 3))}) ON CONFLICT(date, client_type) DO UPDATE SET " +
                     ", ".join(f"{c}=excluded.{c}" for c in cols) + ", fetched_at=excluded.fetched_at",
                     [str(d), r["client_type"]] + [r.get(c) for c in cols] + [now])
    conn.commit()
    return len(rows)


def run(days: int = 7, nse=None) -> dict:
    """Fetch the last `days` calendar days (weekends skipped); already-stored days are not re-fetched."""
    from db.schema import get_connection
    conn = get_connection()
    stored = missing = 0
    unreachable = False
    try:
        ensure_tables(conn)
        have = {str(r[0])[:10] for r in conn.execute("SELECT DISTINCT date FROM fo_participant_oi")}
        for k in range(days):
            d = date.today() - timedelta(days=k)
            if d.weekday() >= 5 or str(d) in have:
                continue
            rows = fetch_day(d, nse)
            if rows is None:
                unreachable = True
                break
            if rows:
                stored += store(conn, d, rows)
            else:
                missing += 1
    finally:
        conn.close()
    if unreachable and not stored:
        return {"status": "FAILED", "rows": 0, "error": "NSE archives unreachable"}
    return {"status": "SUCCESS" if stored else "SKIPPED", "rows": stored, "missing_days": missing,
            "reason": None if stored else "nothing new published"}


def main(argv=None):
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m data.participant_oi")
    ap.add_argument("--days", type=int, default=30)
    print(json.dumps(run(ap.parse_args(argv).days), indent=2))


if __name__ == "__main__":
    main()
