"""
Tenant-owned user workspace: watchlists, alert rules, saved reports.

enterprise_watchlist     watchlist_id, tenant_id, user_id, name, symbols_json, shared (tenant-wide)
enterprise_alert_rule    rule_id, tenant_id, user_id, name, symbol, feature (any W3/W6 feature
                         name, validated), op (< <= > >= ==), value, status ACTIVE / PAUSED,
                         last_value, last_triggered_at
enterprise_report        report_id, tenant_id, user_id, name, kind (strategy_performance /
                         risk_exposure / factor_ranking / backtest_summary / custom),
                         params_json, shared

evaluate_alerts(conn, as_of): after the close, computes each ACTIVE rule's feature
point in time (W3 EvalEnv) and sends an "alert" notification to the rule's owner
when the condition holds (once per day per rule). Informational only.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "==": lambda a, b: a == b}
REPORT_KINDS = ("strategy_performance", "risk_exposure", "factor_ranking", "backtest_summary", "custom")


def _id(p):
    return p + uuid.uuid4().hex[:12].upper()


def save_watchlist(conn, tenant_id, user_id, name, symbols, shared=False, watchlist_id=None) -> dict:
    syms = sorted({s.strip().upper() for s in symbols or [] if s and s.strip()})
    if not name or len(syms) > 500:
        raise ValueError("name required; at most 500 symbols")
    wid = watchlist_id or _id("W")
    conn.execute("INSERT INTO enterprise_watchlist (watchlist_id,tenant_id,user_id,name,symbols_json,shared,created_at,"
                 "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(watchlist_id) DO UPDATE SET name=excluded.name,"
                 "symbols_json=excluded.symbols_json,shared=excluded.shared,updated_at=excluded.updated_at",
                 (wid, tenant_id, user_id, name, json.dumps(syms), int(bool(shared)), datetime.now(), datetime.now()))
    conn.commit()
    return get_owned(conn, "enterprise_watchlist", "watchlist_id", wid, tenant_id, user_id)


def save_alert(conn, tenant_id, user_id, name, symbol, feature, op, value, rule_id=None) -> dict:
    from strategy_engine.features import known
    if not known(feature):
        raise ValueError(f"unknown feature {feature!r}")
    if op not in OPS:
        raise ValueError(f"op must be one of {sorted(OPS)}")
    rid = rule_id or _id("A")
    conn.execute("INSERT INTO enterprise_alert_rule (rule_id,tenant_id,user_id,name,symbol,feature,op,value,status,"
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET name=excluded.name,"
                 "symbol=excluded.symbol,feature=excluded.feature,op=excluded.op,value=excluded.value",
                 (rid, tenant_id, user_id, name or f"{symbol} {feature} {op} {value}", symbol.upper(), feature, op,
                  float(value), "ACTIVE", datetime.now()))
    conn.commit()
    return get_owned(conn, "enterprise_alert_rule", "rule_id", rid, tenant_id, user_id)


def save_report(conn, tenant_id, user_id, name, kind, params=None, shared=False, report_id=None) -> dict:
    if kind not in REPORT_KINDS:
        raise ValueError(f"kind must be one of {REPORT_KINDS}")
    rid = report_id or _id("R")
    conn.execute("INSERT INTO enterprise_report (report_id,tenant_id,user_id,name,kind,params_json,shared,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(report_id) DO UPDATE SET name=excluded.name,kind=excluded.kind,"
                 "params_json=excluded.params_json,shared=excluded.shared",
                 (rid, tenant_id, user_id, name, kind, json.dumps(params or {}), int(bool(shared)), datetime.now()))
    conn.commit()
    return get_owned(conn, "enterprise_report", "report_id", rid, tenant_id, user_id)


def get_owned(conn, table, key, value, tenant_id, user_id) -> dict:
    r = conn.execute(f"SELECT * FROM {table} WHERE {key}=? AND tenant_id=? AND (user_id=? OR shared=1)",
                     (value, tenant_id, user_id)).fetchone() if table != "enterprise_alert_rule" else \
        conn.execute(f"SELECT * FROM {table} WHERE {key}=? AND tenant_id=? AND user_id=?",
                     (value, tenant_id, user_id)).fetchone()
    if not r:
        raise ValueError(f"no {table.replace('enterprise_', '')} {value}")
    d = dict(r)
    for k in ("symbols_json", "params_json"):
        if k in d:
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
    return d


def list_owned(conn, table, tenant_id, user_id) -> list:
    shared = " OR shared=1" if table != "enterprise_alert_rule" else ""
    out = []
    for r in conn.execute(f"SELECT * FROM {table} WHERE tenant_id=? AND (user_id=?{shared}) ORDER BY created_at DESC",
                          (tenant_id, user_id)):
        d = dict(r)
        for k in ("symbols_json", "params_json"):
            if k in d:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
        out.append(d)
    return out


def delete_owned(conn, table, key, value, tenant_id, user_id) -> None:
    n = conn.execute(f"DELETE FROM {table} WHERE {key}=? AND tenant_id=? AND user_id=?",
                     (value, tenant_id, user_id)).rowcount
    if not n:
        raise ValueError(f"no {table.replace('enterprise_', '')} {value} owned by you")
    conn.commit()


def evaluate_alerts(conn, as_of=None) -> dict:
    from backtest.data import PriceHistory, ScoresHistory
    from enterprise.notifications import notify
    from strategy_engine.kinds import EvalEnv
    from strategy_engine.regime import get_provider
    rules = [dict(r) for r in conn.execute("SELECT * FROM enterprise_alert_rule WHERE status='ACTIVE'")]
    if not rules:
        return {"rules": 0, "triggered": 0}
    if as_of is None:
        r = conn.execute("SELECT MAX(date) FROM prices_daily WHERE source IN ('dhan','bhavcopy')").fetchone()[0]
        as_of = r
    as_of = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    syms = sorted({r["symbol"] for r in rules})
    quant = None
    try:
        from quant.strategy_features import QuantHistory
        quant = QuantHistory(conn, as_of - timedelta(days=10), as_of)
    except Exception:
        pass
    env = EvalEnv(PriceHistory.load(conn, syms, as_of, as_of, warmup_days=420), syms,
                  ScoresHistory(conn, as_of - timedelta(days=10), as_of), get_provider(conn, as_of - timedelta(days=10),
                                                                                       as_of), {}, None, quant)
    fired = 0
    for r in rules:
        ctx = env.context(r["symbol"], as_of)
        v = ctx.get(r["feature"]) if ctx is not None else None
        conn.execute("UPDATE enterprise_alert_rule SET last_value=? WHERE rule_id=?",
                     (v if isinstance(v, (int, float)) else None, r["rule_id"]))
        if isinstance(v, (int, float)) and OPS[r["op"]](v, r["value"]) and \
                str(r.get("last_triggered_at") or "")[:10] != str(as_of):
            notify(conn, r["tenant_id"], "alert", f"{r['name']}",
                   f"{r['symbol']} {r['feature']} = {v:.4g} {r['op']} {r['value']} on {as_of}", user_ids=[r["user_id"]])
            conn.execute("UPDATE enterprise_alert_rule SET last_triggered_at=? WHERE rule_id=?", (datetime.now(),
                                                                                                  r["rule_id"]))
            fired += 1
    conn.commit()
    return {"rules": len(rules), "triggered": fired, "as_of": str(as_of)}
