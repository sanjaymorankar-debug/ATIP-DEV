"""ATIP — News Fetcher + Claude API Sentiment Classifier"""
import json, logging, argparse, re, time, os
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from dotenv import load_dotenv
from db.schema import get_connection, log_job

load_dotenv()

log = logging.getLogger(__name__)
try: import feedparser; HAS_FP=True
except ImportError: HAS_FP=False; log.warning("pip install feedparser")
try: import anthropic; HAS_CLAUDE=True
except ImportError: HAS_CLAUDE=False

RSS_FEEDS=[
    {"name":"MoneyControl","url":"https://www.moneycontrol.com/rss/business.xml","weight":1.0},
    {"name":"Economic Times","url":"https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms","weight":1.0},
    {"name":"Business Standard","url":"https://www.business-standard.com/rss/markets-106.rss","weight":0.9},
    {"name":"LiveMint","url":"https://www.livemint.com/rss/markets","weight":0.9},
    {"name":"Google News India Markets","url":"https://news.google.com/rss/search?q=NSE+OR+BSE+OR+Nifty+stock+market+when:1d&hl=en-IN&gl=IN&ceid=IN:en","weight":0.8},
    # Added 2026-09-24 (NS-01) so one dead feed cannot stop news. Not yet
    # confirmed from this machine: news_source_status records whether each
    # answers, and a feed that keeps failing shows there with its error.
    {"name":"Economic Times Stocks","url":"https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms","weight":1.0},
    {"name":"Business Standard Companies","url":"https://www.business-standard.com/rss/companies-101.rss","weight":0.9},
    {"name":"Hindu BusinessLine Markets","url":"https://www.thehindubusinessline.com/markets/feeder/default.rss","weight":0.8},
    {"name":"Financial Express Market","url":"https://www.financialexpress.com/market/feed/","weight":0.8},
]
SYMBOLS={"RELIANCE":["Reliance","RIL"],"TCS":["TCS","Tata Consultancy"],"INFY":["Infosys"],
         "HDFCBANK":["HDFC Bank"],"ICICIBANK":["ICICI Bank"],"TATAMOTORS":["Tata Motors"],
         "TATASTEEL":["Tata Steel"],"ADANIENT":["Adani Enterprises"],"ZOMATO":["Zomato"],
         "PAYTM":["Paytm"],"VEDL":["Vedanta"],"DLF":["DLF"],"BEL":["Bharat Electronics"],
         "HAL":["HAL","Hindustan Aeronautics"],"RECLTD":["REC Ltd"],"PFC":["Power Finance"],
         "BHEL":["BHEL"],"SAIL":["SAIL","Steel Authority"],"SUNPHARMA":["Sun Pharma"]}

SENTIMENT_PROMPT="""Analyse this Indian financial news headline. Return ONLY JSON:
{{"sentiment":<float -1 to 1>,"importance":<"LOW"|"MEDIUM"|"HIGH">,"confidence":<float 0-1>,"category":<"EARNINGS"|"ORDERS"|"M&A"|"RBI"|"GOVT"|"GLOBAL"|"GEOPOLITICS"|"SECTOR"|"GENERAL">,"ai_summary":<max 80 chars>}}
Headline: {headline}"""

def _local_time(utc_struct):
    """A feedparser UTC time.struct_time as a naive local (IST) datetime."""
    import calendar
    return datetime.fromtimestamp(calendar.timegm(utc_struct))

def fetch_feeds(hours_back=12):
    if not HAS_FP: return []
    # Times are IST local, like every other time ATIP stores. feedparser's
    # published_parsed is UTC; it used to be stored as it came, so
    # news_articles.fetched_at was UTC while the recency in compute_news_score
    # was measured from the local clock -- every article read 5.5 hours older
    # than it was -- and the day windows scoring reads cut at UTC midnight.
    cutoff=datetime.now()-timedelta(hours=hours_back); articles=[]; seen=set()
    status={}
    for feed in RSS_FEEDS:
        n=0; err=None
        try:
            from ops.resilience import breaker as _br     # W31 (OPS-10): a dead feed is skipped fast
            f=_br("news:"+feed["name"],failure_threshold=3,reset_seconds=1800).call(feedparser.parse,feed["url"])
            if getattr(f,"bozo",0) and not f.entries:
                err=f"unreadable feed: {getattr(f,'bozo_exception','')}"[:300]
            elif getattr(f,"status",200)>=400:
                err=f"HTTP {f.status}"
            for e in f.entries:
                pub=_local_time(e.published_parsed) if getattr(e,"published_parsed",None) else datetime.now()
                if pub<cutoff: continue
                headline=e.get("title","").strip()
                if not headline: continue
                key=headline_key(headline)
                if key in seen: continue
                seen.add(key); n+=1
                articles.append({"headline":headline,"url":e.get("link",""),"source":feed["name"],"published":pub,"summary":e.get("summary","")[:300]})
        except Exception as ex:
            err=str(ex)[:300]; log.warning(f"  Feed {feed['name']}: {ex}")
        status[feed["name"]]={"url":feed["url"],"items":n,"error":err}
    _record_feed_status(status)
    dead=[k for k,v in status.items() if v["error"]]
    log.info(f"  ✓ {len(articles)} articles fetched from {len(status)-len(dead)}/{len(status)} feeds"
             + (f" — failing: {', '.join(dead)}" if dead else ""))
    return articles

def headline_key(headline):
    """What makes two headlines the same story: letters and digits, lower case,
    first 40 -- the key fetch_feeds de-duplicated on within one fetch."""
    return re.sub(r"\W+","",headline.lower())[:40]

def _record_feed_status(status):
    """news_source_status: per feed, when it last answered and how many items it
    gave, so a feed that goes dead is visible instead of silently empty."""
    try:
        conn=get_connection(); now=datetime.now()
        try:
            for name,v in status.items():
                ok=v["error"] is None
                conn.execute("""INSERT INTO news_source_status (source,url,last_attempt,last_ok,last_items,consecutive_failures,last_error)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET url=excluded.url,last_attempt=excluded.last_attempt,
                    last_ok=COALESCE(excluded.last_ok,news_source_status.last_ok),last_items=excluded.last_items,
                    consecutive_failures=CASE WHEN excluded.last_error IS NULL THEN 0 ELSE news_source_status.consecutive_failures+1 END,
                    last_error=excluded.last_error""",
                    (name,v["url"],now,now if ok else None,v["items"],0 if ok else 1,v["error"]))
            conn.commit()
        finally: conn.close()
    except Exception as e: log.warning(f"  news_source_status not recorded: {e}")

_SYMBOL_ALIASES=None

# Words that appear in company names but match half the market as substrings.
_ALIAS_STOPWORDS={"LTD","LIMITED","INDIA","INDIAN","THE","AND","CO","CORP","CORPORATION",
                  "COMPANY","GROUP","HOLDINGS","ENTERPRISES","INDUSTRIES","SERVICES",
                  "TECHNOLOGIES","FINANCE","FINANCIAL","BANK","MOTORS","STEEL","POWER",
                  "ENERGY","PHARMA","PHARMACEUTICALS","CEMENT","CHEMICALS","LABS",
                  "LABORATORIES","PRODUCTS","INTERNATIONAL","NATIONAL","OF","NEW"}

def build_symbol_aliases(conn=None):
    """
    symbol -> [name fragments to look for in a headline].

    The old detect_symbols() matched against a hardcoded dict of 19 symbols, so
    news could only ever attach to those 19 — measured on this database, 11% of
    1,124 articles linked to any stock and only 15 distinct symbols ever
    matched. For the other ~487 tracked names the news component of
    VPI/MRI/RRI/CRI/ZPI silently fell back to a flat neutral 50.

    This derives aliases for the whole tracked universe from the Dhan security
    master (already downloaded for quotes/orders — no new data source), keeping
    the curated SYMBOLS entries as high-quality overrides for the awkward cases
    ("RIL" for Reliance, "HAL" for Hindustan Aeronautics).

    Single generic words are dropped: matching "BANK" or "POWER" as a company
    name would attach market-wide headlines to arbitrary stocks, which is worse
    than no attribution at all.
    """
    global _SYMBOL_ALIASES
    if _SYMBOL_ALIASES is not None:
        return _SYMBOL_ALIASES
    aliases={sym:list(kws) for sym,kws in SYMBOLS.items()}
    try:
        from data.companies import load_company_names
        for sym,name in (load_company_names() or {}).items():
            if not name: continue
            sym=sym.upper().strip()
            # Trim the legal suffix, then require something distinctive left.
            words=[w for w in re.split(r"[^A-Za-z0-9&]+",name.upper()) if w]
            core=[w for w in words if w not in _ALIAS_STOPWORDS]
            cand=" ".join(core).strip()
            if len(cand)<4 or (len(core)==1 and core[0] in _ALIAS_STOPWORDS):
                continue
            aliases.setdefault(sym,[])
            if cand not in (a.upper() for a in aliases[sym]):
                aliases[sym].append(cand)
    except Exception as e:
        log.warning(f"  symbol aliases: falling back to curated list only ({e})")
    _SYMBOL_ALIASES=aliases
    log.info(f"  ✓ News symbol aliases: {len(aliases)} symbols")
    return aliases

def detect_symbols(text):
    tu=text.upper()
    return [sym for sym,kws in build_symbol_aliases().items()
            if any(k.upper() in tu for k in kws)]

def classify_rule_based(art):
    h=art["headline"].lower()
    pos=sum(1 for w in ["profit","growth","surge","rise","order","win","record","rally","beat"] if w in h)
    neg=sum(1 for w in ["loss","decline","fall","weak","cut","crash","risk","miss","fraud"] if w in h)
    s=min(0.3+pos*0.15,1.0) if pos>neg else max(-0.3-neg*0.15,-1.0) if neg>pos else 0.0
    # confidence 0.5 is a placeholder, not a measurement: classifier='rule'
    # keeps it out of ACS's NewsConfidence (scores/engine.get_news_confidence)
    art.update({"sentiment":round(s,2),"importance":"MEDIUM","confidence":0.5,"category":"GENERAL","ai_summary":art["headline"][:80],
                "classifier":"rule"})
    return art

def classify_with_claude(articles, batch_size=10):
    """W28: delegates to data/news_ai.classify -- batched Claude Haiku 4.5 with a
    daily cost cap and a measured rule fallback (config.json "news"). The per-
    headline implementation below is kept only for reference and is unused."""
    from data.news_ai import classify
    return classify(articles)

def _classify_with_claude_legacy(articles, batch_size=10):
    if not HAS_CLAUDE: return [classify_rule_based(a) for a in articles]
    api_key=os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — falling back to rule-based sentiment for all articles")
        return [classify_rule_based(a) for a in articles]
    client=anthropic.Anthropic(api_key=api_key); results=[]
    for i in range(0,len(articles),batch_size):
        batch=articles[i:i+batch_size]
        for art in batch:
            try:
                resp=client.messages.create(model="claude-sonnet-5",max_tokens=200,
                    messages=[{"role":"user","content":SENTIMENT_PROMPT.format(headline=art["headline"])}])
                text=re.sub(r"```json|```","",resp.content[0].text).strip()
                data=json.loads(text)
                art.update({"sentiment":float(data.get("sentiment",0)),"importance":data.get("importance","MEDIUM"),
                            "confidence":float(data.get("confidence",0.7)),"category":data.get("category","GENERAL"),
                            "ai_summary":data.get("ai_summary",art["headline"][:80]),"classifier":"claude"})
            except Exception as ex:
                log.warning(f"  Claude sentiment call failed for '{art['headline'][:50]}...': {ex}")
                art=classify_rule_based(art)
            results.append(art); time.sleep(0.3)
        log.info(f"  Classified {min(i+batch_size,len(articles))}/{len(articles)}")
    return results

def compute_news_score(sentiment, importance, confidence, recency_hours):
    iw={"HIGH":1.0,"MEDIUM":0.7,"LOW":0.4}.get(importance,0.7)
    return round(0.50*((sentiment+1)/2*100)+0.20*(iw*100)+0.20*max(0,100-recency_hours*(100/24))+0.10*(confidence*100),2)

def store_articles(articles, conn):
    """Store articles not already stored. INSERT OR IGNORE had no unique key to
    ignore on, so the overlapping pre-market (14h) and midday (12h) windows
    stored the same story twice: 142 of 1,249 stored headlines were repeats.
    An article is skipped when its URL, or its headline key, was stored in the
    last 3 days."""
    count=0; now=datetime.now()
    since=now-timedelta(days=3)
    known_urls={r[0] for r in conn.execute("SELECT url FROM news_articles WHERE fetched_at>=? AND url<>''",(since,))}
    known_keys={headline_key(r[0]) for r in conn.execute("SELECT headline FROM news_articles WHERE fetched_at>=?",(since,))}
    for art in articles:
        try:
            if (art.get("url") and art["url"] in known_urls) or headline_key(art["headline"]) in known_keys:
                continue
            known_urls.add(art.get("url")); known_keys.add(headline_key(art["headline"]))
            pub=art.get("published",now); recency=(now-pub).total_seconds()/3600
            syms=detect_symbols(art["headline"]+" "+art.get("summary",""))
            for s in art.get("ai_symbols") or []:          # W28: symbols the model named
                if s not in syms: syms.append(s)
            ns=compute_news_score(art.get("sentiment",0),art.get("importance","MEDIUM"),art.get("confidence",0.5),recency)
            conn.execute("INSERT OR IGNORE INTO news_articles (fetched_at,headline,source,url,category,symbols_mentioned,sentiment,importance,confidence,news_score,ai_summary,processed,classifier) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (pub.strftime("%Y-%m-%d %H:%M:%S"),art["headline"],art.get("source",""),art.get("url",""),
                 art.get("category","GENERAL"),json.dumps(syms),art.get("sentiment",0),art.get("importance","MEDIUM"),
                 art.get("confidence",0.5),ns,art.get("ai_summary",art["headline"][:80]),art.get("classifier")))
            count+=1
        except Exception as e: log.warning(f"  Article store: {e}")
    return count

def run_news_pipeline(hours_back=12):
    log.info(f"📰 News pipeline (last {hours_back}h)")
    conn=get_connection(); result={"rows":0,"status":"SUCCESS"}
    try:
        articles=fetch_feeds(hours_back)
        articles=classify_with_claude(articles)
        rows=store_articles(articles,conn); conn.commit(); result["rows"]=rows
        result["fetched"]=len(articles)
        # Fetched but all already stored is a working feed, not an empty job
        if rows==0 and articles: result["status"]="NO_NEW"
        log.info(f"  ✓ {rows} new articles stored ({len(articles)-rows} already had)")
        log_job("news","SUCCESS",rows)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}"); log_job("news","FAILED",0,error=e)
    finally: conn.close()
    return result

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--hours",type=int,default=12)
    args=ap.parse_args(); run_news_pipeline(args.hours)
