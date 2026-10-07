"""
SQL-level tenant scoping (W9, W7-R4).

W7 enforced tenant isolation in the HTTP middleware only. This module gives every
query -- API, CLI or scheduled job -- one way to scope a table to a tenant:

    TABLES                     classification of every ATIP table
    scope(table, tenant, alias)   -> (sql_fragment, params) for a WHERE clause
    select(conn, table, tenant, ...)   scoped SELECT helper
    current_tenant()           the tenant of the running request / job (ops/context.py),
                               default tenant when none is set
    tenant_job(tenant)         context manager: run job code as one tenant
    for_each_tenant(conn, fn)  run fn(conn, tenant_id) once per ACTIVE tenant
    isolation_check(conn)      what is and is not scoped (python -m enterprise isolation-check)

Classes:
    GLOBAL          shared market / reference data (prices, scores, news, factors ...)
    OWNER           the owner's W1 book and broker account (paper_*, order_*, holdings ...):
                    visible to the default tenant only
    DIRECT          a tenant_id column is authoritative
    VIA_STRATEGY    owned through strategy.tenant_id (the W3/W4 tenant_id columns on
                    intents / risk decisions / orders default to 'default', so the
                    strategy decides -- same rule as enterprise/authz.py)
    VIA_MODEL       owned through ml_model.tenant_id
    VIA_PARENT      owned through a parent row (backtest runs, orders, quant portfolios, pairs)
    PLATFORM        operations tables (ops_*, pipeline_log, schema_migrations): platform admin only
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

GLOBAL, OWNER, DIRECT, VIA_STRATEGY, VIA_MODEL, VIA_PARENT, PLATFORM = (
    "GLOBAL", "OWNER", "DIRECT", "VIA_STRATEGY", "VIA_MODEL", "VIA_PARENT", "PLATFORM")

_G = ("prices_daily ai_scores technical_indicators market_health index_levels live_quotes live_ticks news_articles "
      "news_source_status fii_dii_market global_markets bulk_deals corporate_actions fundamental_data "
      "institutional_data predictions accuracy_tracker signal_log signal_outcome weight_config derivatives_instrument "
      "derivatives_quote options_analytics market_event microstructure_feature quant_factor quant_factor_set "
      "quant_composite quant_factor_score ml_feature ml_feature_set ml_label data_quality enterprise_plan "
      "enterprise_role enterprise_permission enterprise_role_permission")
_O = ("paper_account paper_order paper_position order_log order_rules portfolio_holdings portfolio_sync pnl_daily "
      "strategy_position strategy_event alert_log risk_limit risk_limit_history")
_D = ("strategy ml_model quant_pair quant_experiment quant_portfolio backtest_run ml_dataset quant_factor_research "
      "enterprise_tenant enterprise_user_role enterprise_session enterprise_api_key enterprise_notification "
      "enterprise_audit enterprise_subscription enterprise_usage enterprise_invoice enterprise_watchlist "
      "enterprise_alert_rule enterprise_report enterprise_refresh_token ops_webhook_endpoint tenant_paper_account "
      "tenant_paper_position tenant_paper_fill tenant_pnl_daily enterprise_vault_credential enterprise_onboarding "
      "enterprise_payment enterprise_notification_pref enterprise_notification_delivery enterprise_report_output "
      "enterprise_consent enterprise_privacy_request enterprise_api_usage")
_VS = ("strategy_decision strategy_decision_run strategy_engine_event strategy_feature strategy_health "
       "strategy_parameter strategy_regime_mapping strategy_version oms_fill oms_order risk_decision "
       "strategy_position_intent")
_VM = "ml_model_event ml_model_explanation ml_model_metrics ml_model_monitoring ml_model_version ml_prediction " \
      "ml_training_run"
_P = ("ops_alert ops_backup ops_config_version ops_heartbeat ops_idempotency ops_job_lock ops_secret_access "
      "ops_secret_meta ops_webhook_delivery ops_webhook_event pipeline_log schema_migrations sqlite_sequence "
      "enterprise_user enterprise_password_reset enterprise_email_token enterprise_risk_profile")
# W32: tables added in W11-W31, classified (they were unclassified -> fail closed for tenants)
_G += (" fo_underlying_daily formula_registry fundamental_filing global_market_history insider_trade intraday_bars "
       "sast_disclosure score_components sector_breadth shareholding_pattern technical_ext event_study news_summary "
       "quant_factor_approval ml_anomaly ml_cluster ml_cluster_run intraday_scan_hit")
_O += (" live_pnl_snapshot paper_futures_position paper_futures_trade portfolio_optimization portfolio_rebalance_plan "
       "portfolio_risk_snapshot risk_emergency_exit reconciliation_run research_study research_link ml_validation_report")
# wealth/ rows carry (tenant_id, owner_id): the tenant column is authoritative
_D += (" investor_profile investor_profile_version perf_ledger perf_ledger_void perf_report_run wealth_advice_log "
       "wealth_allocation_policy wealth_allocation_run wealth_classification wealth_cycle_run wealth_feedback "
       "wealth_goal wealth_goal_event wealth_goal_projection wealth_holding wealth_liability wealth_preference "
       "wealth_rebalance_plan wealth_snapshot")
# W39: history backfill progress and research reports (owner research, shared reference data)
_G += (" prices_daily_backfill research_report technical_snapshot technical_signal order_book_pressure "
       "fo_participant_oi market_cue market_regime_gate fundamental_scorecard macro_event")
_O += " research_screen"                       # the owner's saved screens
_P += (" ai_usage_log audit_export broker_health_check ops_restore_drill ops_rollback_drill ml_health_check "
       "live_feed_status enterprise_mfa_recovery")
# child -> (parent table, child column, parent column)
PARENTS = {
    "backtest_drawdown": ("backtest_run", "run_id", "run_id"), "backtest_equity": ("backtest_run", "run_id", "run_id"),
    "backtest_montecarlo": ("backtest_run", "run_id", "run_id"), "backtest_trade": ("backtest_run", "run_id", "run_id"),
    "oms_execution": ("oms_order", "order_id", "order_id"), "oms_order_event": ("oms_order", "order_id", "order_id"),
    "quant_exposure": ("quant_portfolio", "portfolio_id", "portfolio_id"),
    "quant_portfolio_position": ("quant_portfolio", "portfolio_id", "portfolio_id"),
    "quant_spread": ("quant_pair", "pair_id", "pair_id"),
}

TABLES = {}
for _cls, _names in ((GLOBAL, _G), (OWNER, _O), (DIRECT, _D), (VIA_STRATEGY, _VS), (VIA_MODEL, _VM), (PLATFORM, _P)):
    for _n in _names.split():
        TABLES[_n] = _cls
for _n in PARENTS:
    TABLES[_n] = VIA_PARENT


def default_tenant() -> str:
    try:
        from enterprise.config import settings
        return settings()["default_tenant"]
    except Exception:
        return "default"


def _col(alias, c):
    return f"{alias}.{c}" if alias else c


def scope(table: str, tenant: str, alias: str | None = None) -> tuple:
    """(WHERE fragment, params) restricting `table` to `tenant`. Unknown tables fail closed."""
    cls = TABLES.get(table)
    own = tenant == default_tenant()
    if cls == GLOBAL:
        return "1=1", ()
    if cls in (OWNER, PLATFORM):
        return ("1=1", ()) if own else ("1=0", ())
    if cls == DIRECT:
        return f"COALESCE({_col(alias, 'tenant_id')},'default')=?", (tenant,)
    if cls == VIA_STRATEGY:
        return (f"{_col(alias, 'strategy_id')} IN (SELECT strategy_id FROM strategy WHERE "
                f"COALESCE(tenant_id,'default')=?)"), (tenant,)
    if cls == VIA_MODEL:
        return (f"{_col(alias, 'model_id')} IN (SELECT model_id FROM ml_model WHERE "
                f"COALESCE(tenant_id,'default')=?)"), (tenant,)
    if cls == VIA_PARENT:
        parent, ccol, pcol = PARENTS[table]
        frag, params = scope(parent, tenant, "p")
        return f"{_col(alias, ccol)} IN (SELECT p.{pcol} FROM {parent} p WHERE {frag})", params
    return "1=0", ()                                   # unclassified: fail closed


def select(conn, table, tenant, columns="*", where="", params=(), order="", limit=None) -> list:
    if not re.match(r"^[a-z_]+$", table):
        raise ValueError("bad table name")
    frag, sp = scope(table, tenant)
    q = f"SELECT {columns} FROM {table} WHERE ({frag})" + (f" AND ({where})" if where else "")
    q += f" ORDER BY {order}" if order else ""
    q += f" LIMIT {int(limit)}" if limit else ""
    return [dict(r) for r in conn.execute(q, tuple(sp) + tuple(params)).fetchall()]


def owns(conn, table, key_col, key, tenant) -> bool:
    frag, sp = scope(table, tenant)
    return conn.execute(f"SELECT 1 FROM {table} WHERE {key_col}=? AND ({frag})", (key,) + tuple(sp)).fetchone() \
        is not None


def current_tenant() -> str:
    try:
        from ops.context import tenant_id
        return tenant_id.get() or default_tenant()
    except Exception:
        return default_tenant()


@contextmanager
def tenant_job(tenant):
    from ops.context import tenant_id
    tok = tenant_id.set(tenant)
    try:
        yield tenant
    finally:
        tenant_id.reset(tok)


def active_tenants(conn) -> list:
    try:
        rows = [r[0] for r in conn.execute("SELECT tenant_id FROM enterprise_tenant WHERE status='ACTIVE' ORDER BY "
                                           "tenant_id")]
    except Exception:
        rows = []
    return rows or [default_tenant()]


def for_each_tenant(conn, fn) -> dict:
    out = {}
    for t in active_tenants(conn):
        with tenant_job(t):
            try:
                out[t] = fn(conn, t)
            except Exception as e:
                out[t] = {"error": f"{type(e).__name__}: {e}"}
    return out


# -- isolation check ----------------------------------------------------------------------

_SQL_FROM = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_]+)", re.I)
TENANT_HINTS = ("tenant", "scope(", "scoping.", "strategy_id=?", "strategy_id =", "model_id=?", "_owned", "owned",
                "user_id=?")


def isolation_check(conn, root=".") -> dict:
    """
    1. every DB table classified (unclassified tables are listed; scope() fails closed on them)
    2. every DIRECT table has a tenant_id column
    3. a static review list: source lines that read / write a tenant-owned table without a
       visible tenant / owner filter on the same line. This is a HEURISTIC review aid, not
       a proof of isolation: HTTP access to these paths is filtered by enterprise/authz.py;
       CLI and scheduled jobs are platform-level (they act for every tenant by design).
    """
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    unclassified = sorted(t for t in tables if t not in TABLES)
    missing_col = []
    for t, cls in TABLES.items():
        if cls == DIRECT and t in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            if "tenant_id" not in cols:
                missing_col.append(t)
    owned = {t for t, c in TABLES.items() if c in (DIRECT, VIA_STRATEGY, VIA_MODEL, VIA_PARENT)}
    review = []
    for p in sorted(Path(root).rglob("*.py")):
        s = str(p).replace("\\", "/")
        if any(x in s for x in ("/tests/", "/.venv/", "site-packages", "enterprise/scoping.py", "db/schema.py",
                                "ops/migrations.py", "tools/")):
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            hits = {m.group(1).lower() for m in _SQL_FROM.finditer(line)} & owned
            if hits and not any(h in line for h in TENANT_HINTS):
                review.append({"file": s.split("ATIP-dev/")[-1].split("ATIP/")[-1], "line": i,
                               "tables": sorted(hits)})
    by_module = {}
    for r in review:
        mod = r["file"].split("/")[0]
        by_module[mod] = by_module.get(mod, 0) + 1
    return {"tables": len(tables), "classified": len([t for t in tables if t in TABLES]),
            "unclassified": unclassified, "direct_tables_missing_tenant_id": missing_col,
            "unscoped_query_lines": len(review), "by_module": by_module, "review": review[:500],
            "note": "heuristic review list, not a proof; HTTP paths are filtered by enterprise/authz.py"}
