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
]
SYMBOLS={"RELIANCE":["Reliance","RIL"],"TCS":["TCS","Tata Consultancy"],"INFY":["Infosys"],
         "HDFCBANK":["HDFC Bank"],"ICICIBANK":["ICICI Bank"],"TATAMOTORS":["Tata Motors"],
         "TATASTEEL":["Tata Steel"],"ADANIENT":["Adani Enterprises"],"ZOMATO":["Zomato"],
         "PAYTM":["Paytm"],"VEDL":["Vedanta"],"DLF":["DLF"],"BEL":["Bharat Electronics"],
         "HAL":["HAL","Hindustan Aeronautics"],"RECLTD":["REC Ltd"],"PFC":["Power Finance"],
         "BHEL":["BHEL"],"SAIL":["SAIL","Steel Authority"],"SUNPHARMA":["Sun Pharma"]}

SENTIMENT_PROMPT="""Analyse this Indian financial news headline. Return ONLY JSON:
{{"sentiment":<float -1 to 1>,"importance":<"LOW"|"MEDIUM"|"HIGH">,"confidence":<float 0-1>,"category":<"EARNINGS"|"ORDERS"|"M&A"|"RBI"|"GOVT"|"GLOBAL"|"SECTOR"|"GENERAL">,"ai_summary":<max 80 chars>}}
Headline: {headline}"""

def fetch_feeds(hours_back=12):
    if not HAS_FP: return []
    cutoff=datetime.utcnow()-timedelta(hours=hours_back); articles=[]; seen=set()
    for feed in RSS_FEEDS:
        try:
            f=feedparser.parse(feed["url"])
            for e in f.entries:
                pub=datetime(*e.published_parsed[:6]) if hasattr(e,"published_parsed") and e.published_parsed else datetime.utcnow()
                if pub<cutoff: continue
                headline=e.get("title","").strip()
                if not headline: continue
                key=re.sub(r"\W+","",headline.lower())[:40]
                if key in seen: continue
                seen.add(key)
                articles.append({"headline":headline,"url":e.get("link",""),"source":feed["name"],"published":pub,"summary":e.get("summary","")[:300]})
        except Exception as ex: log.warning(f"  Feed {feed['name']}: {ex}")
    log.info(f"  ✓ {len(articles)} articles fetched"); return articles

def detect_symbols(text):
    tu=text.upper()
    return [sym for sym,kws in SYMBOLS.items() if any(k.upper() in tu for k in kws)]

def classify_rule_based(art):
    h=art["headline"].lower()
    pos=sum(1 for w in ["profit","growth","surge","rise","order","win","record","rally","beat"] if w in h)
    neg=sum(1 for w in ["loss","decline","fall","weak","cut","crash","risk","miss","fraud"] if w in h)
    s=min(0.3+pos*0.15,1.0) if pos>neg else max(-0.3-neg*0.15,-1.0) if neg>pos else 0.0
    art.update({"sentiment":round(s,2),"importance":"MEDIUM","confidence":0.5,"category":"GENERAL","ai_summary":art["headline"][:80]})
    return art

def classify_with_claude(articles, batch_size=10):
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
                            "ai_summary":data.get("ai_summary",art["headline"][:80])})
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
    count=0; now=datetime.now()
    for art in articles:
        try:
            pub=art.get("published",now); recency=(now-pub).total_seconds()/3600
            syms=detect_symbols(art["headline"]+" "+art.get("summary",""))
            ns=compute_news_score(art.get("sentiment",0),art.get("importance","MEDIUM"),art.get("confidence",0.5),recency)
            conn.execute("INSERT OR IGNORE INTO news_articles (fetched_at,headline,source,url,category,symbols_mentioned,sentiment,importance,confidence,news_score,ai_summary,processed) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                (pub.strftime("%Y-%m-%d %H:%M:%S"),art["headline"],art.get("source",""),art.get("url",""),
                 art.get("category","GENERAL"),json.dumps(syms),art.get("sentiment",0),art.get("importance","MEDIUM"),
                 art.get("confidence",0.5),ns,art.get("ai_summary",art["headline"][:80])))
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
        log.info(f"  ✓ {rows} articles stored"); log_job("news","SUCCESS",rows)
    except Exception as e:
        conn.rollback(); result["status"]="FAILED"; log.error(f"  ✗ {e}"); log_job("news","FAILED",0,error=e)
    finally: conn.close()
    return result

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    ap=argparse.ArgumentParser(); ap.add_argument("--hours",type=int,default=12)
    args=ap.parse_args(); run_news_pipeline(args.hours)
