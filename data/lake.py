"""
Point-in-time data lake (W35: DP-22).

High-volume and research datasets (ticks, depth snapshots, full F&O bhavcopies, option
chains, alt data, macro vintages, archived table snapshots) do not belong in the operational
SQLite file. They go here:

    atip_data/lake/<dataset>/date=<YYYY-MM-DD>/<part>.<fmt>

    fmt     parquet when pyarrow is installed, else csv.gz (identical content, slower to read).
            `pip install pyarrow duckdb` switches new writes to Parquet and enables query().
    manifest  lake_partition: one row per file -- dataset, partition date, path, rows, columns,
            sha256, format, source, and KNOWLEDGE TIME (when this information became known to
            ATIP / was public). Nothing is ever overwritten: a correction is a new part with a
            later knowledge time.

Point-in-time reads
    read(dataset, start, end, as_of=None)
        as_of=None   every part (latest knowledge)
        as_of=T      only parts whose knowledge_time <= T; when a partition was rewritten, only
                     the parts of the newest VERSION known by T (a backtest at T sees the data as
                     it stood at T, not as later corrected)

    write(dataset, part_date, df, knowledge_time=None, source="", replace=False)
        replace=False appends a part to the partition's current version (ticks arrive in many
        flushes); replace=True starts a new version (a corrected / re-downloaded day)

    archive_table(table, date_col, dataset, start, end)   copy SQLite rows into the lake
    query(sql)                                            DuckDB over the lake (when installed)
    stats()                                               datasets, partitions, rows, bytes
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(os.environ.get("ATIP_LAKE_ROOT", "atip_data/lake"))


def _fmt() -> str:
    try:
        import pyarrow  # noqa: F401
        return "parquet"
    except ImportError:
        return "csv.gz"


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def _conn(conn):
    if conn is not None:
        return conn, False
    from db.schema import get_connection
    return get_connection(), True


def _current_version(conn, dataset, d) -> int:
    r = conn.execute("SELECT MAX(version) FROM lake_partition WHERE dataset=? AND partition_date=?",
                     (dataset, str(d))).fetchone()
    return int(r[0] or 0)


def write(dataset: str, part_date, df: pd.DataFrame, knowledge_time=None, source: str = "", replace: bool = False,
          conn=None) -> dict:
    if df is None or df.empty:
        return {"dataset": dataset, "rows": 0}
    if not dataset.replace("_", "").isalnum():
        raise ValueError("dataset names are letters, digits and underscores")
    d = _d(part_date)
    conn, own = _conn(conn)
    try:
        ver = _current_version(conn, dataset, d)
        ver = ver + 1 if (replace or ver == 0) else ver
        fmt = _fmt()
        folder = ROOT / dataset / f"date={d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"v{ver}-{datetime.now():%H%M%S}-{uuid.uuid4().hex[:6]}.{fmt}"
        if fmt == "parquet":
            df.to_parquet(path, index=False)
            data = path.read_bytes()
        else:
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            data = gzip.compress(buf.getvalue().encode("utf-8"))
            path.write_bytes(data)
        kt = knowledge_time or datetime.now()
        conn.execute("INSERT INTO lake_partition (dataset,partition_date,version,path,format,`rows`,columns_json,sha256,"
                     "bytes,source,knowledge_time,written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (dataset, str(d), ver, str(path), fmt, int(len(df)), json.dumps(list(map(str, df.columns))),
                      hashlib.sha256(data).hexdigest(), len(data), source, str(kt)[:19], datetime.now()))
        conn.commit()
        return {"dataset": dataset, "date": str(d), "version": ver, "rows": int(len(df)), "path": str(path),
                "format": fmt}
    finally:
        if own:
            conn.close()


def _read_file(path: str, fmt: str) -> pd.DataFrame:
    if fmt == "parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, compression="gzip")


def parts(dataset, start=None, end=None, as_of=None, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        sql = ("SELECT partition_date, version, path, format, `rows`, knowledge_time FROM lake_partition WHERE dataset=?")
        args = [dataset]
        if start:
            sql += " AND partition_date>=?"
            args.append(str(_d(start)))
        if end:
            sql += " AND partition_date<=?"
            args.append(str(_d(end)))
        if as_of is not None:
            sql += " AND knowledge_time<=?"
            args.append(str(as_of)[:19])
        rows = conn.execute(sql + " ORDER BY partition_date, version, written_at", args).fetchall()
    finally:
        if own:
            conn.close()
    newest = {}
    for pd_, ver, *_ in rows:
        newest[pd_] = max(newest.get(pd_, 0), ver)
    return [dict(zip(("date", "version", "path", "format", "rows", "knowledge_time"), r)) for r in rows
            if r[1] == newest[r[0]]]


def read(dataset, start=None, end=None, as_of=None, columns=None, conn=None) -> pd.DataFrame:
    frames = []
    for p in parts(dataset, start, end, as_of, conn):
        try:
            df = _read_file(p["path"], p["format"])
        except FileNotFoundError:
            log.warning(f"  lake: {p['path']} is in the manifest but missing on disk")
            continue
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        df["_partition_date"] = p["date"]
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def archive_table(table: str, date_col: str, dataset: str | None = None, start=None, end=None, conn=None,
                  knowledge_col: str | None = None) -> dict:
    """Copy rows of a SQLite table into the lake, one partition per day. knowledge_col (e.g.
    fundamental_data.available_from) sets each partition's knowledge time to its latest value."""
    if not table.replace("_", "").isalnum() or not date_col.replace("_", "").isalnum():
        raise ValueError("bad table / column name")
    conn, own = _conn(conn)
    try:
        sql = f"SELECT * FROM {table} WHERE 1=1"
        args = []
        if start:
            sql += f" AND {date_col}>=?"
            args.append(str(start))
        if end:
            sql += f" AND {date_col}<=?"
            args.append(f"{end} 23:59:59" if len(str(end)) == 10 else str(end))
        df = pd.read_sql(sql, conn, params=args)
        if df.empty:
            return {"table": table, "partitions": 0, "rows": 0}
        df["_day"] = df[date_col].astype(str).str[:10]
        n = rows = 0
        for day, g in df.groupby("_day"):
            kt = None
            if knowledge_col and knowledge_col in g and g[knowledge_col].notna().any():
                kt = str(g[knowledge_col].dropna().astype(str).max())[:19]
            write(dataset or table, day, g.drop(columns=["_day"]), knowledge_time=kt or f"{day} 23:59:59",
                  source=f"sqlite:{table}", replace=True, conn=conn)
            n += 1
            rows += len(g)
        return {"table": table, "partitions": n, "rows": rows}
    finally:
        if own:
            conn.close()


def query(sql: str):
    """DuckDB over the lake. Datasets are exposed as views named after them (parquet or csv.gz)."""
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError("pip install duckdb to query the lake with SQL; read() works without it") from e
    con = duckdb.connect()
    for ds in [p.name for p in ROOT.iterdir() if p.is_dir()] if ROOT.exists() else []:
        pq = list((ROOT / ds).glob("date=*/*.parquet"))
        cs = list((ROOT / ds).glob("date=*/*.csv.gz"))
        if pq:
            con.execute(f"CREATE VIEW {ds} AS SELECT * FROM read_parquet('{(ROOT / ds).as_posix()}/date=*/*.parquet', "
                        f"hive_partitioning=true)")
        elif cs:
            con.execute(f"CREATE VIEW {ds} AS SELECT * FROM read_csv_auto('{(ROOT / ds).as_posix()}/date=*/*.csv.gz', "
                        f"hive_partitioning=true)")
    return con.execute(sql).df()


def stats(conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        rows = [dict(zip(("dataset", "partitions", "files", "rows", "bytes", "first", "last", "formats"), r)) for r in
                conn.execute("SELECT dataset, COUNT(DISTINCT partition_date), COUNT(*), SUM(`rows`), SUM(bytes), "
                             "MIN(partition_date), MAX(partition_date), GROUP_CONCAT(DISTINCT format) FROM lake_partition "
                             "GROUP BY dataset ORDER BY dataset")]
        return {"root": str(ROOT), "write_format": _fmt(), "datasets": rows}
    finally:
        if own:
            conn.close()


def verify(dataset=None, conn=None) -> dict:
    """Re-hash every file against the manifest."""
    conn, own = _conn(conn)
    bad, ok = [], 0
    try:
        sql = "SELECT path, sha256 FROM lake_partition" + (" WHERE dataset=?" if dataset else "")
        for path, sha in conn.execute(sql, (dataset,) if dataset else ()).fetchall():
            try:
                h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except FileNotFoundError:
                bad.append({"path": path, "problem": "missing"})
                continue
            if h != sha:
                bad.append({"path": path, "problem": "hash mismatch"})
            else:
                ok += 1
        return {"ok": ok, "problems": bad}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="ATIP point-in-time data lake (DP-22)")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--archive", nargs=2, metavar=("TABLE", "DATE_COL"))
    ap.add_argument("--knowledge-col")
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()
    if a.archive:
        print(archive_table(a.archive[0], a.archive[1], start=a.start, end=a.end, knowledge_col=a.knowledge_col))
    if a.verify:
        print(verify())
    if a.stats or not (a.archive or a.verify):
        print(json.dumps(stats(), indent=2, default=str))
