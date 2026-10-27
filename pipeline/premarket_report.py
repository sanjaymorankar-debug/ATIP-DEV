"""
Pre-market report (Task 2 step 5 / Task 3.5, 2026-10-26).

Built by the 06:00 IST resume sequence and again at 08:15, so a current one exists before the 08:30 morning
brief (alerts/telegram.py send_morning_digest, which now carries the Asia line too) and the 09:15 open.
One JSON document per IST date and label, in premarket_reports and atip_data/reports/premarket_<date>.json.

    market      Market Health (latest scored session), its regime and inputs; MSI
    indexes     the universe mean of VPI, ZPI, MRI, RRI, CRI, ACS and the BUY / SELL / HOLD counts
    signals     top BUY and SELL names with every index, the Trade of the Day
    asia        the Asia Sentiment Score, each market's signal / % change / session, the Pakistan risk flag
    global      US / Europe closes, crude, gold, USD-INR, US 10Y, the global score; GIFT Nifty
    news        the latest summary's tone and whether Claude or the keyword fallback classified it
    news_impact the News Impact Estimate for the session (data/news_impact): score, bias, sectors, drivers, brief
    outlook     MH, the global score and the Asia score combined (0.50 / 0.25 / 0.25, renormalised over
                what is present) -- informational, not a trading rule
    caveats     every input that is missing or stale, in words

Nothing here places an order.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
REPORT_DIR = Path("atip_data/reports")
OUTLOOK_WEIGHTS = {"mh": 0.50, "global": 0.25, "asia": 0.25}

DDL = """CREATE TABLE IF NOT EXISTS premarket_reports (
    for_date TEXT NOT NULL, label TEXT NOT NULL, generated_at TIMESTAMP, report_json TEXT,
    PRIMARY KEY (for_date, label))"""


def _news_ai_on() -> bool:
    try:
        from data.news_ai import settings
        return bool(settings()["ai_enabled"])
    except Exception:
        return False


def _one(conn, q, *a):
    try:
        r = conn.execute(q, a).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _many(conn, q, *a):
    try:
        return [dict(r) for r in conn.execute(q, a).fetchall()]
    except Exception:
        return []


def build(conn, now: datetime | None = None) -> dict:
    now = (now or datetime.now(IST)).astimezone(IST)
    caveats = []
    sd = (_one(conn, "SELECT MAX(date) d FROM ai_scores") or {}).get("d")
    sd = str(sd)[:10] if sd else None
    if not sd:
        caveats.append("no scored session yet")
    mh = _one(conn, "SELECT date, mh_score, regime, mh_coverage, mh_inputs FROM market_health "
                    "WHERE mh_score IS NOT NULL ORDER BY date DESC LIMIT 1") or {}
    idx = _one(conn, "SELECT AVG(vpi) vpi, AVG(zpi) zpi, AVG(mri) mri, AVG(rri) rri, AVG(cri) cri, AVG(acs) acs, "
                     "AVG(msi) msi, SUM(`signal`='BUY') buys, SUM(`signal`='SELL') sells, SUM(`signal`='HOLD') holds, "
                     "COUNT(*) n FROM ai_scores WHERE date=?", sd) if sd else None
    cols = "symbol, atip_score, vpi, zpi, mri, rri, cri, acs"
    buys = _many(conn, f"SELECT {cols} FROM ai_scores WHERE date=? AND `signal`='BUY' ORDER BY atip_score DESC LIMIT 5", sd) if sd else []
    sells = _many(conn, f"SELECT {cols} FROM ai_scores WHERE date=? AND `signal`='SELL' ORDER BY cri DESC LIMIT 5", sd) if sd else []
    tod = _one(conn, "SELECT symbol, acs, atip_score FROM ai_scores WHERE date=? AND is_tod=1 LIMIT 1", sd) if sd else None
    try:
        from data import asia_markets
        asia = asia_markets.snapshot(conn, now)
    except Exception as e:
        asia = {"markets": [], "sentiment": None, "error": str(e)}
    sent = asia.get("sentiment") or {}
    if not sent:
        caveats.append("no Asia Sentiment Score yet")
    elif str(sent.get("date")) != str(now.date()):
        caveats.append(f"Asia Sentiment Score is from {sent.get('date')}")
    failed = [m["code"] for m in asia.get("markets", []) if not m.get("last")]
    if failed:
        caveats.append("no data for " + ", ".join(failed))
    g = _one(conn, "SELECT date, time, sp500_chg, dow_chg, nasdaq_chg, ftse100_chg, dax_chg, crude_brent, "
                   "crude_brent_chg, gold, gold_chg, usd_inr, usd_inr_chg, us_10y, global_score, global_sentiment "
                   "FROM global_markets ORDER BY date DESC, id DESC LIMIT 1") or {}
    if not g:
        caveats.append("no global markets fetch stored")
    gift = _one(conn, "SELECT date, time, gift_nifty_chg FROM index_levels WHERE gift_nifty_chg IS NOT NULL "
                      "ORDER BY date DESC, time DESC LIMIT 1")
    ns = _one(conn, "SELECT created_at, classifier, fallback_reason, summary_json FROM news_summary ORDER BY id DESC LIMIT 1") or {}
    tone = None
    if ns.get("summary_json"):
        try:
            tone = json.loads(ns["summary_json"]).get("tone")
        except Exception:
            tone = None
    if ns.get("classifier") == "rule" and _news_ai_on():     # rules by choice (news.ai_enabled false) is no caveat
        caveats.append("news sentiment came from the keyword fallback, not Claude")
    try:                                          # NI-04: the news impact estimate, informational
        from data import news_impact
        ni = news_impact.latest(conn)
    except Exception:
        ni = None
    if not ni:
        caveats.append("no news impact estimate yet")
    elif ni.get("for_session") != news_impact.target_session(now):
        caveats.append(f"news impact estimate is for {ni.get('for_session')}")
    parts = {"mh": mh.get("mh_score"), "global": g.get("global_score"), "asia": sent.get("score")}
    used = {k: v for k, v in parts.items() if v is not None}
    outlook = None
    if used:
        w = sum(OUTLOOK_WEIGHTS[k] for k in used)
        outlook = round(sum(float(v) * OUTLOOK_WEIGHTS[k] for k, v in used.items()) / w, 1)
    return {"for_date": str(now.date()), "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "scores_date": sd,
            "market": {"mh": mh, "msi": (idx or {}).get("msi")},
            "indexes": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in (idx or {}).items()},
            "signals": {"buys": buys, "sells": sells, "trade_of_day": tod},
            "asia": {"sentiment": {k: sent.get(k) for k in ("date", "run_label", "score", "signal", "confidence",
                                                            "coverage", "pk_risk_level")} if sent else None,
                     "pakistan_risk": sent.get("pk_risk") if sent else None,
                     "markets": [{k: m.get(k) for k in ("code", "name", "last", "chg_pct", "signal", "confidence",
                                                        "session_status", "date", "source")}
                                 for m in asia.get("markets", [])]},
            "global": g, "gift_nifty": gift,
            "news_impact": None if not ni else {k: ni.get(k) for k in (
                "for_session", "run_label", "generated_at", "score", "bias", "strength", "n_items", "n_claude",
                "n_rule", "sectors", "drivers", "brief", "brief_method")},
            "news": {"tone": tone, "classifier": ns.get("classifier"), "fallback_reason": ns.get("fallback_reason"),
                     "at": str(ns.get("created_at") or "")[:16] or None},
            "outlook": {"score": outlook, "label": (None if outlook is None else "POSITIVE" if outlook >= 60
                                                    else "NEGATIVE" if outlook <= 40 else "MIXED"),
                        "inputs": used, "weights": OUTLOOK_WEIGHTS, "informational_only": True},
            "caveats": caveats}


def build_and_store(now: datetime | None = None, label: str = "resume") -> dict:
    from db.schema import get_connection
    now = (now or datetime.now(IST)).astimezone(IST)
    conn = get_connection()
    try:
        conn.execute(DDL)
        rep = build(conn, now)
        conn.execute("INSERT INTO premarket_reports (for_date,label,generated_at,report_json) VALUES (?,?,?,?) "
                     "ON CONFLICT(for_date,label) DO UPDATE SET generated_at=excluded.generated_at, "
                     "report_json=excluded.report_json",
                     (rep["for_date"], label, now.replace(tzinfo=None), json.dumps(rep, default=str)))
        conn.commit()
    except Exception as e:
        log.error(f"  pre-market report: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()
    path = None
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"premarket_{rep['for_date']}.json"
        path.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        log.warning(f"  pre-market report file: {e}")
    log.info(f"  📝 Pre-market report {rep['for_date']} ({label}): outlook {rep['outlook']['score']} "
             f"{rep['outlook']['label']}; {len(rep['caveats'])} caveat(s)")
    return {"ok": True, "report": rep, "path": str(path) if path else None}


def latest(conn) -> dict | None:
    try:
        conn.execute(DDL)
        r = conn.execute("SELECT report_json FROM premarket_reports ORDER BY generated_at DESC LIMIT 1").fetchone()
    except Exception:
        return None
    return json.loads(r[0]) if r else None


def run_job(label: str = "refresh") -> dict:
    r = build_and_store(label=label)
    return {"status": "SUCCESS" if r.get("ok") else "FAILED", "rows": 1 if r.get("ok") else 0, "error": r.get("error")}
