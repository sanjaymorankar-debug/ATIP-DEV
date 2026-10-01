"""
Widget JSON schemas (W36: API-06).

Every dashboard widget reads one JSON endpoint. WIDGETS names the widget, the endpoint it reads and
the JSON Schema (draft 2020-12 subset) of that endpoint's response; the schema files live in
dashboard/schemas/<widget>.json. They are the contract a future front end (or the mobile app,
ENT-09) codes against, and the check that keeps the API from drifting silently.

    GET /api/schemas                 the catalogue (widget, title, endpoint, description)
    GET /api/schemas/{widget}        one schema
    GET /api/schemas/{widget}/check  fetch the live endpoint (GET, same server) and validate it
    GET /api/schemas/check-all       every widget, a pass / fail list

validate(instance, schema) -> [errors] supports the keywords the schemas use: type (incl. a list of
types), anyOf, required, properties, additionalProperties, items, enum, minimum, maximum, minItems.
No third-party jsonschema package is needed. infer(sample) drafts a schema from a sample response
(leaf values nullable, required = the keys of the sample's top level) -- how the first versions of
these files were produced; review a drafted schema before committing it.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

WIDGETS = {
    "market_health":   ("Market health card", "/api/mh", "Regime, Market Health score and its inputs (main dashboard header)"),
    "trade_of_day":    ("Trade of the day", "/api/tod", "The top TOD pick with its scores"),
    "scores_table":    ("ATIP scores table", "/api/scores", "Per-stock scores and signal for the latest session"),
    "news_list":       ("News tab", "/api/news", "Latest classified headlines"),
    "news_brief":      ("Market brief card", "/api/news/summary", "AI or rule-based market brief (News tab)"),
    "news_ai_usage":   ("News AI usage", "/api/news/ai-usage", "News-AI spend against the daily cap"),
    "news_weights":    ("News weight by stock", "/api/news/symbol-scores", "Weighted news score per stock (NS-05)"),
    "alerts_panel":    ("Alerts and job health", "/api/alerts", "Pipeline problems and recent alerts"),
    "portfolio":       ("Portfolio tab", "/api/portfolio", "Synced holdings with P&L and scores"),
    "crash_risk":      ("Crash risk 25", "/api/lists/crash-risk", "Highest-CRI stocks (Lists & scans tab)"),
    "top_spi":         ("Top SPI 25", "/api/lists/spi", "Highest Stability Profit Index (Lists & scans tab)"),
    "msi_series":      ("MSI series", "/api/lists/msi", "Market Sentiment Index history and components"),
    "intraday_scans":  ("Intraday scans", "/api/scans/intraday", "Latest intraday scan hits"),
    "live_pnl":        ("Live P&L", "/api/pnl/live", "LIVE and PAPER book P&L snapshot"),
    "feed_status":     ("Live feed status", "/api/market/feed-status", "Stock and index feed state"),
    "strategy_perf":   ("Strategy performance", "/api/strategy-performance", "Per-strategy decisions and book P&L"),
}


def catalogue() -> list:
    return [{"widget": w, "title": t, "endpoint": e, "description": d, "schema": f"/api/schemas/{w}"}
            for w, (t, e, d) in WIDGETS.items()]


def get(widget: str) -> dict:
    if widget not in WIDGETS:
        raise LookupError(f"unknown widget {widget}")
    return json.loads((SCHEMA_DIR / f"{widget}.json").read_text(encoding="utf-8"))


# ── validation ────────────────────────────────────────────────────────────
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t):
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(inst, schema: dict, path: str = "$") -> list:
    errs = []
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(inst, x) for x in ts):
            return [f"{path}: expected {'/'.join(ts)}, got {type(inst).__name__}"]
    if "anyOf" in schema:
        branches = [validate(inst, b, path) for b in schema["anyOf"]]
        if all(branches):
            return [f"{path}: matches none of {len(branches)} alternatives (first: {branches[0][:2]})"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append(f"{path}: {inst} < minimum {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append(f"{path}: {inst} > maximum {schema['maximum']}")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing required '{k}'")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property '{k}'")
            elif isinstance(schema.get("additionalProperties"), dict):
                errs += validate(v, schema["additionalProperties"], f"{path}.{k}")
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(inst[:500]):
                errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


# ── drafting ──────────────────────────────────────────────────────────────
def infer(sample, top=True):
    if isinstance(sample, dict):
        return {"type": "object" if top else ["object", "null"],
                "properties": {k: infer(v, False) for k, v in sample.items()},
                **({"required": sorted(sample)} if top else {})}
    if isinstance(sample, list):
        merged = {}
        for x in sample[:50]:
            if isinstance(x, dict):
                for k, v in x.items():
                    if k not in merged or merged[k] is None:
                        merged[k] = v
        item = infer(merged, False) if merged else ({} if not sample else infer(sample[0], False))
        if isinstance(item.get("type"), list) and "object" in item["type"]:
            item["type"] = "object"
        return {"type": "array" if top else ["array", "null"], "items": item}
    if isinstance(sample, bool):
        return {"type": ["boolean", "null"]}
    if isinstance(sample, (int, float)):
        return {"type": ["number", "null"]}
    if isinstance(sample, str):
        return {"type": ["string", "null"]}
    return {}                                     # null in the sample: any type


def draft(widget: str, sample) -> dict:
    t, e, d = WIDGETS[widget]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"atip/widgets/{widget}.json",
            "title": t, "description": f"{d}. Endpoint: GET {e}.", **infer(sample)}
