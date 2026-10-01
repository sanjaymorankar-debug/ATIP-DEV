"""
Alternative-data framework (W35: AD-01).

A source is a class with an id, a description, the entity it is keyed on ('symbol' or 'market'),
its metrics, and one method:

    class MySource(DataSource):
        source_id = "my_source"; entity = "symbol"; metrics = ("value",)
        def fetch(self, conn, as_of, entities) -> list[Observation]: ...

    @register   (on the class)  makes it runnable by id

Every Observation carries an available_from: the moment the value could have been known. Nothing is
stored without one, and every read is point in time (available_from <= as_of), so an alt-data series can
be used in research and strategies without look-ahead.

    run_source(conn, source_id, as_of)   fetch -> alt_observation (+ lake "alt_<source_id>", DP-22),
                                         health in alt_dataset: rows, coverage (entities with data /
                                         entities asked), days since the newest observation, error
    run_enabled(as_of)                   every source listed in config.json altdata.enabled (scheduler)
    read(conn, source_id, metric, ...)   point-in-time rows
    panel(conn, source_id, metric, start, end, as_of)  date x entity DataFrame for quant research
                                         (e.g. quant.research information-coefficient studies)
    zscore(conn, ...)                    cross-sectional z-score of one metric on one date

Sources never write to scores or strategies directly: a source becomes a signal only after research
(IC, decay) and the factor approval gate (W22, AF-08).
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime

log = logging.getLogger(__name__)

REGISTRY: dict = {}


@dataclass
class Observation:
    entity: str
    date: date
    metric: str
    value: float | None
    available_from: datetime
    meta: dict = field(default_factory=dict)


class DataSource:
    source_id: str = "base"
    name: str = ""
    description: str = ""
    entity: str = "symbol"          # 'symbol' | 'market'
    frequency: str = "D"
    metrics: tuple = ()
    external: bool = False          # calls a third-party service

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def entities(self, conn) -> list:
        """Default universe: the tracked symbols (capped by altdata.max_entities)."""
        if self.entity == "market":
            return ["MARKET"]
        from data.dhan import get_tracked_symbols
        cap = int(self.cfg.get("max_entities") or 500)
        return sorted(get_tracked_symbols(conn))[:cap]

    def fetch(self, conn, as_of: date, entities: list) -> list:
        raise NotImplementedError


def register(cls):
    REGISTRY[cls.source_id] = cls
    return cls


def settings() -> dict:
    from pathlib import Path
    try:
        cfg = json.loads((Path("atip_data") / "config.json").read_text(encoding="utf-8"))
        a = cfg.get("altdata") or {}
    except Exception:
        a = {}
    return {"enabled": a.get("enabled", ["news_attention", "announcement_intensity"]),
            "max_entities": a.get("max_entities", 500), "sources": a.get("sources") or {}}


def _load():
    import altdata.sources  # noqa: F401  (registers the built-in sources)


def make(source_id: str):
    _load()
    if source_id not in REGISTRY:
        raise LookupError(f"no alt-data source {source_id}; registered: {sorted(REGISTRY)}")
    s = settings()
    cfg = {"max_entities": s["max_entities"], **(s["sources"].get(source_id) or {})}
    return REGISTRY[source_id](cfg)


def _health(conn, src, status, rows, coverage, err=None):
    newest = conn.execute("SELECT MAX(date) FROM alt_observation WHERE source_id=?", (src.source_id,)).fetchone()[0]
    stale = (date.today() - date.fromisoformat(str(newest)[:10])).days if newest else None
    conn.execute("INSERT INTO alt_dataset (source_id,name,description,entity,frequency,enabled,last_run,last_status,"
                 "last_rows,coverage,stale_days,error,meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) "
                 "DO UPDATE SET name=excluded.name, description=excluded.description, last_run=excluded.last_run, "
                 "last_status=excluded.last_status, last_rows=excluded.last_rows, coverage=excluded.coverage, "
                 "stale_days=excluded.stale_days, error=excluded.error, enabled=excluded.enabled, meta_json=excluded.meta_json",
                 (src.source_id, src.name, src.description, src.entity, src.frequency,
                  int(src.source_id in settings()["enabled"]), datetime.now(), status, rows, coverage, stale, err,
                  json.dumps({"metrics": list(src.metrics), "external": src.external})))
    conn.commit()


def run_source(conn, source_id: str, as_of=None) -> dict:
    src = make(source_id)
    as_of = date.fromisoformat(str(as_of)[:10]) if as_of else date.today()
    ents = src.entities(conn)
    try:
        obs = src.fetch(conn, as_of, ents) or []
    except Exception as e:
        _health(conn, src, "FAILED", 0, 0.0, f"{type(e).__name__}: {e}"[:400])
        raise
    good = [o for o in obs if o.available_from is not None and o.metric in src.metrics]
    for o in good:
        conn.execute("INSERT OR REPLACE INTO alt_observation (source_id,entity,date,metric,value,available_from,meta_json) "
                     "VALUES (?,?,?,?,?,?,?)", (source_id, o.entity, str(o.date), o.metric, o.value,
                                               str(o.available_from)[:19], json.dumps(o.meta) if o.meta else None))
    conn.commit()
    if good:
        import pandas as pd
        from data import lake
        lake.write(f"alt_{source_id}", as_of, pd.DataFrame([{"entity": o.entity, "date": str(o.date), "metric": o.metric,
                                                             "value": o.value, "available_from": str(o.available_from)}
                                                            for o in good]), source=source_id, conn=conn)
    covered = len({o.entity for o in good})
    cov = round(covered / len(ents), 4) if ents else 0.0
    _health(conn, src, "SUCCESS" if good else "EMPTY", len(good), cov,
            None if len(good) == len(obs) else f"{len(obs) - len(good)} observations without availability / unknown metric")
    return {"source_id": source_id, "rows": len(good), "entities": len(ents), "covered": covered, "coverage": cov}


def run_enabled(as_of=None) -> dict:
    from db.schema import get_connection, log_job
    conn = get_connection()
    out, failed = [], []
    try:
        for sid in settings()["enabled"]:
            try:
                out.append(run_source(conn, sid, as_of))
            except Exception as e:
                failed.append(f"{sid}: {e}")
                log.warning(f"  alt data {sid}: {e}")
        rows = sum(r["rows"] for r in out)
        status = "FAILED" if failed and not out else ("PARTIAL" if failed else "SUCCESS")
        log_job("alt_data", status, rows, error="; ".join(failed) or None)
        return {"status": status, "rows": rows, "sources": out, "failed": failed}
    finally:
        conn.close()


def read(conn, source_id, metric, entity=None, start=None, end=None, as_of=None) -> list:
    sql = ("SELECT entity, date, value, available_from FROM alt_observation WHERE source_id=? AND metric=? "
           "AND available_from<=?")
    if as_of is None:
        cutoff = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif len(str(as_of)) <= 10:
        cutoff = f"{str(as_of)[:10]} 23:59:59"          # a date: everything known by the end of it
    else:
        cutoff = str(as_of)[:19]
    args = [source_id, metric, cutoff]
    if entity:
        sql += " AND entity=?"
        args.append(entity.upper())
    if start:
        sql += " AND date>=?"
        args.append(str(start)[:10])
    if end:
        sql += " AND date<=?"
        args.append(str(end)[:10])
    return [dict(zip(("entity", "date", "value", "available_from"), r)) for r in conn.execute(sql + " ORDER BY date", args)]


def panel(conn, source_id, metric, start, end, as_of=None):
    import pandas as pd
    rows = read(conn, source_id, metric, None, start, end, as_of)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.pivot_table(index="date", columns="entity", values="value", aggfunc="last").sort_index()


def zscore(conn, source_id, metric, d, as_of=None) -> dict:
    vals = {r["entity"]: r["value"] for r in read(conn, source_id, metric, None, d, d, as_of) if r["value"] is not None}
    if len(vals) < 3:
        return {}
    mu, sd = statistics.mean(vals.values()), statistics.pstdev(vals.values())
    return {k: round((v - mu) / sd, 4) if sd else 0.0 for k, v in vals.items()}


def catalogue(conn) -> dict:
    _load()
    health = {r[0]: dict(zip(("source_id", "last_run", "last_status", "last_rows", "coverage", "stale_days", "error"), r))
              for r in conn.execute("SELECT source_id, last_run, last_status, last_rows, coverage, stale_days, error "
                                    "FROM alt_dataset")}
    en = settings()["enabled"]
    return {"sources": [{"source_id": k, "name": c.name, "description": c.description, "entity": c.entity,
                         "metrics": list(c.metrics), "external": c.external, "enabled": k in en,
                         "health": health.get(k)} for k, c in sorted(REGISTRY.items())]}
