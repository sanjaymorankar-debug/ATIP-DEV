"""
One-off repair (DBS-06): move news_articles.fetched_at from UTC to IST.

Until 2026-09-24 data/news.py stored each article's feed publication time as
feedparser gives it -- UTC -- while every other time ATIP stores is IST local.
The code now converts (data/news.py _local_time); this moves the rows stored
before that by +5h30m.

Which rows: those stored in UTC. created_at is SQLite's CURRENT_TIMESTAMP, also
UTC and written moments after the fetch, so for a UTC-stored row fetched_at is
at or before created_at; an IST-stored row is ~5h30m after it. A row counts as
UTC when fetched_at <= created_at + 1 hour. That makes the repair safe to run
twice: a moved row no longer qualifies.

    python -m tools.repair_news_timezone            # dry run: counts only
    python -m tools.repair_news_timezone --apply    # after backing up atip.db

SQLITE ONLY. Both statements use SQLite date modifiers that db.postgres does
not translate: datetime(created_at, '+1 hour') and
strftime(fmt, fetched_at, '+5 hours', '+30 minutes'). The one that matters is
safe -- strftime() is on the translator's unsupported list, so the --apply
UPDATE raises UnsupportedSQL and cannot half-convert the column. The dry run's
datetime() passes through untranslated and fails at the server instead, which
is loud but not a refusal.

This is a one-off repair for rows written before 2026-09-24, so a database
migrated to PostgreSQL after that date has nothing for it to fix. If it is ever
needed there, do the arithmetic in Python over a SELECT and write the rows back.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.schema import get_connection  # noqa: E402

UTC_ROWS = "fetched_at <= datetime(created_at, '+1 hour')"


def main(apply: bool) -> int:
    conn = get_connection()
    try:
        n, lo, hi = conn.execute(f"SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM news_articles "
                                 f"WHERE {UTC_ROWS}").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        print(f"{n} of {total} articles look UTC-stored ({lo} → {hi})")
        if not apply:
            print("dry run — nothing changed. Back up atip_data/atip.db, then re-run with --apply.")
            return n
        cur = conn.execute(f"UPDATE news_articles SET fetched_at = "
                           f"strftime('%Y-%m-%d %H:%M:%S', fetched_at, '+5 hours', '+30 minutes') "
                           f"WHERE {UTC_ROWS}")
        conn.commit()
        print(f"moved {cur.rowcount} rows to IST")
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    main(ap.parse_args().apply)
