"""
ATIP -- NSE corporate announcements + earnings-call / announcement NLP (W28: NS-06).

    fetch(from_d, to_d)          NSE's corporate-announcements feed for a date range
    classify_announcement(row)   subject + text -> event type, importance, tone (rules +
                                 the NS-03 finance lexicon); every row gets this
    analyse_document(...)        for the announcements worth reading in full --
                                 earnings-call transcripts, results outcomes, investor
                                 presentations of TRACKED stocks -- the attached PDF is
                                 sent to Claude (news_ai budget, purpose 'announcement')
                                 for tone, guidance direction, key points and risks
    run_announcements(days)      the scheduled job: store new rows, analyse up to
                                 MAX_DOCS_PER_RUN documents

Stored in corporate_announcement (one row per NSE announcement, ann_id = NSE seq id or a
hash). classifier is 'rules' or 'claude'; nlp_json holds the document analysis when one
ran. Announcement tone feeds nothing automatically: it is shown on /insights and is
available to research (quant/events.py can read the table as an event source).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
       "&from_date={f}&to_date={t}")
MAX_DOCS_PER_RUN = 10
MAX_PDF_BYTES = 15 * 1024 * 1024

# subject keywords -> (event type, importance)
SUBJECT_RULES = [
    ("transcript", ("EARNINGS_CALL", "HIGH")),
    ("earnings call", ("EARNINGS_CALL", "HIGH")),
    ("analysts/institutional investor meet", ("INVESTOR_MEET", "MEDIUM")),
    ("investor presentation", ("INVESTOR_MEET", "MEDIUM")),
    ("financial result", ("RESULTS", "HIGH")),
    ("outcome of board meeting", ("BOARD_OUTCOME", "HIGH")),
    ("board meeting", ("BOARD_MEETING", "LOW")),
    ("dividend", ("DIVIDEND", "MEDIUM")),
    ("buy back", ("BUYBACK", "HIGH")), ("buyback", ("BUYBACK", "HIGH")),
    ("bonus", ("BONUS_SPLIT", "MEDIUM")), ("split", ("BONUS_SPLIT", "MEDIUM")),
    ("acquisition", ("M_AND_A", "HIGH")), ("amalgamation", ("M_AND_A", "HIGH")), ("scheme of arrangement", ("M_AND_A", "HIGH")),
    ("credit rating", ("CREDIT_RATING", "MEDIUM")),
    ("award of order", ("ORDER_WIN", "MEDIUM")), ("bagging", ("ORDER_WIN", "MEDIUM")), ("receipt of order", ("ORDER_WIN", "MEDIUM")),
    ("resignation", ("MGMT_CHANGE", "MEDIUM")), ("appointment", ("MGMT_CHANGE", "LOW")), ("cessation", ("MGMT_CHANGE", "MEDIUM")),
    ("pledge", ("PROMOTER_ACTIVITY", "MEDIUM")), ("insider trading", ("PROMOTER_ACTIVITY", "LOW")),
    ("litigation", ("LEGAL", "MEDIUM")), ("dispute", ("LEGAL", "MEDIUM")),
    ("penalty", ("REGULATORY_ACTION", "MEDIUM")), ("show cause", ("REGULATORY_ACTION", "MEDIUM")),
    ("fund raising", ("FUNDRAISE", "MEDIUM")), ("qualified institutions placement", ("FUNDRAISE", "MEDIUM")),
    ("allotment", ("FUNDRAISE", "LOW")),
    ("agm", ("AGM", "LOW")), ("annual general meeting", ("AGM", "LOW")),
    ("shareholders meeting", ("AGM", "LOW")), ("newspaper publication", ("FILING", "LOW")),
    ("trading window", ("FILING", "LOW")), ("certificate", ("FILING", "LOW")),
]
DEEP_EVENTS = {"EARNINGS_CALL", "RESULTS", "BOARD_OUTCOME", "INVESTOR_MEET"}

DOC_SYSTEM = (
    "You analyse an Indian listed company's stock-exchange filing (an earnings-call transcript, results "
    "outcome or investor presentation). Report only what the document states. Return: summary (3-5 "
    "sentences), tone (-1.0 clearly negative for the business outlook to 1.0 clearly positive), "
    "confidence (0-1, how clearly the document supports that tone), guidance (RAISED, LOWERED, MAINTAINED, "
    "INITIATED or NONE -- forward guidance on revenue, margins or growth as stated by management), "
    "key_points (up to 6), risks (up to 5, as stated or clearly implied by management), and "
    "numbers (up to 6 short strings quoting stated figures such as revenue growth or margin). No advice."
)
DOC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "tone", "confidence", "guidance", "key_points", "risks", "numbers"],
    "properties": {
        "summary": {"type": "string"}, "tone": {"type": "number"}, "confidence": {"type": "number"},
        "guidance": {"type": "string", "enum": ["RAISED", "LOWERED", "MAINTAINED", "INITIATED", "NONE"]},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "numbers": {"type": "array", "items": {"type": "string"}},
    },
}


def _dt(v):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(v).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def fetch(from_d: date, to_d: date, session=None) -> list:
    from data.bhavcopy import HEADERS, get_nse_session
    session = session or get_nse_session()
    url = URL.format(f=from_d.strftime("%d-%m-%Y"), t=to_d.strftime("%d-%m-%Y"))
    try:
        r = session.get(url, timeout=30, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code in (401, 403):
            session = get_nse_session()
            r = session.get(url, timeout=30, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code != 200:
            log.warning(f"  announcements: HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        log.warning(f"  announcements: {e}")
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    out = []
    for it in data or []:
        sym = (it.get("symbol") or "").strip().upper()
        if not sym:
            continue
        subj = (it.get("desc") or it.get("subject") or "").strip()
        detail = (it.get("attchmntText") or it.get("smIndustry") or "").strip()
        at = _dt(it.get("an_dt") or it.get("sort_date") or it.get("exchdisstime"))
        seq = it.get("seq_id") or it.get("seqId")
        aid = str(seq) if seq else hashlib.sha1(f"{sym}|{subj}|{at}|{detail[:80]}".encode()).hexdigest()[:20]
        out.append({"ann_id": aid, "symbol": sym, "company": it.get("sm_name"), "broadcast_at": at,
                    "subject": subj, "detail": detail[:4000], "attachment_url": it.get("attchmntFile")})
    return out


def classify_announcement(row: dict) -> dict:
    from data.news_ai import lexicon_sentiment
    subj = (row.get("subject") or "").lower()
    ev, imp = "OTHER", "LOW"
    for kw, (e, i) in SUBJECT_RULES:
        if kw in subj:
            ev, imp = e, i
            break
    tone, conf = lexicon_sentiment(f"{row.get('subject', '')}. {row.get('detail', '')[:600]}")
    row.update({"event_type": ev, "importance": imp, "tone": tone, "confidence": conf,
                "category": "FILING" if ev in ("FILING", "AGM", "BOARD_MEETING") else "CORPORATE",
                "summary": (row.get("detail") or row.get("subject") or "")[:240], "classifier": "rules"})
    return row


def analyse_document(conn, row: dict, client, cfg, session=None) -> bool:
    """Claude reads the attached PDF. True when an analysis was stored."""
    from data import news_ai
    import anthropic
    url = row.get("attachment_url") or ""
    if not url.lower().endswith(".pdf"):
        return False
    from data.bhavcopy import HEADERS, get_nse_session
    session = session or get_nse_session()
    try:
        r = session.get(url, timeout=60, headers=HEADERS)
        if r.status_code != 200 or not r.content or len(r.content) > MAX_PDF_BYTES:
            return False
        pdf = base64.standard_b64encode(r.content).decode()
    except Exception as e:
        log.debug(f"  announcement pdf {url}: {e}")
        return False
    try:
        resp = client.beta.messages.create(
            model=cfg["model"], max_tokens=4000, betas=[news_ai.FALLBACK_BETA], fallbacks="default",
            system=[{"type": "text", "text": DOC_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": cfg.get("effort") or "low", "format": {"type": "json_schema", "schema": DOC_SCHEMA}},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}},
                {"type": "text", "text": f"{row['symbol']} -- {row.get('subject', '')}"}]}],
        )
    except anthropic.APIError as e:
        news_ai.record_usage(conn, "announcement", cfg["model"], error=True)
        log.info(f"  announcement NLP {row['symbol']}: {e.__class__.__name__}")
        return False
    refused = resp.stop_reason == "refusal"
    news_ai.record_usage(conn, "announcement", cfg["model"], resp.usage, 1, refusal=refused)
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if refused or not text:
        return False
    try:
        d = json.loads(text)
    except ValueError:
        return False
    row.update({"tone": max(-1.0, min(1.0, float(d.get("tone") or 0))),
                "confidence": max(0.0, min(1.0, float(d.get("confidence") or 0))),
                "summary": (d.get("summary") or "")[:1200], "classifier": "claude",
                "nlp_json": json.dumps({k: d.get(k) for k in ("guidance", "key_points", "risks", "numbers")}
                                       | {"model": resp.model})})
    return True


def _store(conn, row):
    conn.execute(
        "INSERT INTO corporate_announcement (ann_id,symbol,company,broadcast_at,subject,detail,attachment_url,category,"
        "event_type,tone,importance,confidence,summary,classifier,nlp_json,fetched_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ann_id) DO UPDATE SET tone=excluded.tone,"
        "confidence=excluded.confidence,summary=excluded.summary,classifier=excluded.classifier,"
        "nlp_json=excluded.nlp_json",
        (row["ann_id"], row["symbol"], row.get("company"),
         row["broadcast_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("broadcast_at") else None,
         row.get("subject"), row.get("detail"), row.get("attachment_url"), row.get("category"), row.get("event_type"),
         row.get("tone"), row.get("importance"), row.get("confidence"), row.get("summary"), row.get("classifier"),
         row.get("nlp_json"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def run_announcements(days=1, conn=None, deep=True) -> dict:
    from db.schema import get_connection, log_job
    from data import news_ai
    own = conn is None
    conn = conn or get_connection()
    try:
        from data.bhavcopy import get_nse_session
        session = get_nse_session()
        to_d = date.today()
        rows = fetch(to_d - timedelta(days=max(0, days - 1)), to_d, session)
        have = {r[0] for r in conn.execute("SELECT ann_id FROM corporate_announcement WHERE fetched_at>=?",
                                           ((datetime.now() - timedelta(days=days + 7)).strftime("%Y-%m-%d"),))}
        new = [classify_announcement(r) for r in rows if r["ann_id"] not in have]
        for r in new:
            _store(conn, r)
        conn.commit()
        analysed = 0
        cfg = news_ai.settings()
        if deep and new and cfg["enabled"]:
            client = news_ai.get_client()
            try:
                from data.dhan import get_tracked_symbols
                tracked = set(get_tracked_symbols(conn))
            except Exception:
                tracked = set()
            todo = [r for r in new if r["event_type"] in DEEP_EVENTS and (not tracked or r["symbol"] in tracked)]
            todo.sort(key=lambda r: (r["event_type"] != "EARNINGS_CALL", r["symbol"]))
            for r in todo[:MAX_DOCS_PER_RUN]:
                if client is None or news_ai.spent_today(conn) >= float(cfg["daily_budget_usd"]):
                    break
                if analyse_document(conn, r, client, cfg, session):
                    _store(conn, r)
                    conn.commit()
                    analysed += 1
        log.info(f"  ✓ Announcements: {len(rows)} fetched, {len(new)} new, {analysed} documents analysed")
        log_job("announcements", "SUCCESS", len(new))
        return {"status": "SUCCESS" if rows else "EMPTY", "fetched": len(rows), "new": len(new), "analysed": analysed}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="NSE corporate announcements + NLP (NS-06)")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--no-deep", action="store_true", help="rules only; do not send documents to Claude")
    a = ap.parse_args()
    print(run_announcements(a.days, deep=not a.no_deep))
