"""
Alternative-data sources (W35: AD-02). Registered with altdata.framework.

    news_attention          (internal)  per stock per day from news_articles: article count, the count's
                            z-score vs its own previous 30 days (attention shock), and the mean classified
                            sentiment. available_from = end of that day (23:59) -- a day's count is only
                            known once the day is over.
    announcement_intensity  (internal)  per stock per day from corporate_announcement (W28b, NS-06):
                            material announcements (results, calls, M&A, orders, ratings, management,
                            regulatory) and their mean tone. available_from = the latest broadcast time
                            that day.
    wiki_pageviews          (external, off by default)  Wikimedia REST pageviews of each company's English
                            Wikipedia article: daily views and the views' z-score vs the previous 60 days
                            (retail attention). Article titles come from altdata.sources.wiki_pageviews.map in
                            config.json, else are guessed from the company name and kept only when the API
                            answers. Wikimedia publishes a day's counts after it ends (UTC), so
                            available_from = the next day 06:00 IST. A descriptive User-Agent is sent, as
                            Wikimedia's policy requires.

Enable in config.json: "altdata": {"enabled": ["news_attention", "announcement_intensity", "wiki_pageviews"],
"max_entities": 200, "sources": {"wiki_pageviews": {"max_entities": 100, "map": {"TCS": "Tata_Consultancy_Services"}}}}
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from altdata.framework import DataSource, Observation, register

log = logging.getLogger(__name__)

MATERIAL = {"EARNINGS_CALL", "RESULTS", "BOARD_OUTCOME", "INVESTOR_MEET", "M_AND_A", "BUYBACK", "ORDER_WIN",
            "CREDIT_RATING", "MGMT_CHANGE", "REGULATORY_ACTION", "LEGAL", "FUNDRAISE", "PROMOTER_ACTIVITY"}


def _z(x, hist):
    if len(hist) < 10:
        return None
    sd = statistics.pstdev(hist)
    return round((x - statistics.mean(hist)) / sd, 4) if sd else 0.0


@register
class NewsAttention(DataSource):
    source_id = "news_attention"
    name = "News attention"
    description = "Daily article count per stock, its 30-day z-score, and mean sentiment (news_articles)"
    metrics = ("articles", "articles_z30", "mean_sentiment")

    def fetch(self, conn, as_of, entities):
        start = as_of - timedelta(days=40)
        counts = defaultdict(lambda: defaultdict(int))
        sents = defaultdict(list)
        want = set(entities)
        for fa, sm, s in conn.execute("SELECT fetched_at, symbols_mentioned, sentiment FROM news_articles WHERE "
                                      "fetched_at>=? AND fetched_at<? AND symbols_mentioned NOT IN ('','[]')",
                                      (str(start), str(as_of + timedelta(days=1)))):
            d = str(fa)[:10]
            try:
                syms = json.loads(sm or "[]")
            except ValueError:
                continue
            for sym in syms:
                if sym in want:
                    counts[sym][d] += 1
                    if d == str(as_of) and s is not None:
                        sents[sym].append(float(s))
        out = []
        avail = datetime.combine(as_of, datetime.min.time()).replace(hour=23, minute=59)
        days = [str(start + timedelta(days=i)) for i in range((as_of - start).days)]
        for sym in entities:
            c = counts.get(sym, {})
            today = c.get(str(as_of), 0)
            hist = [c.get(d, 0) for d in days[-30:]]
            out.append(Observation(sym, as_of, "articles", float(today), avail))
            z = _z(today, hist)
            if z is not None:
                out.append(Observation(sym, as_of, "articles_z30", z, avail))
            if sents.get(sym):
                out.append(Observation(sym, as_of, "mean_sentiment", round(statistics.mean(sents[sym]), 4), avail))
        return out


@register
class AnnouncementIntensity(DataSource):
    source_id = "announcement_intensity"
    name = "Announcement intensity"
    description = "Material NSE announcements per stock per day and their mean tone (corporate_announcement)"
    metrics = ("material_count", "mean_tone")

    def fetch(self, conn, as_of, entities):
        want = set(entities)
        try:
            rows = conn.execute("SELECT symbol, broadcast_at, event_type, tone FROM corporate_announcement WHERE "
                                "broadcast_at>=? AND broadcast_at<?", (str(as_of), str(as_of + timedelta(days=1)))).fetchall()
        except Exception:
            return []                       # table absent before W28b
        by = defaultdict(list)
        for sym, at, ev, tone in rows:
            if sym in want and ev in MATERIAL:
                by[sym].append((str(at)[:19], tone))
        out = []
        for sym, items in by.items():
            avail = datetime.fromisoformat(max(a for a, _ in items).replace(" ", "T"))
            out.append(Observation(sym, as_of, "material_count", float(len(items)), avail))
            tones = [t for _, t in items if t is not None]
            if tones:
                out.append(Observation(sym, as_of, "mean_tone", round(statistics.mean(tones), 4), avail))
        return out


@register
class WikiPageviews(DataSource):
    source_id = "wiki_pageviews"
    name = "Wikipedia pageviews"
    description = "Daily English-Wikipedia pageviews of each company's article and their 60-day z-score"
    metrics = ("views", "views_z60")
    external = True
    API = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
           "{title}/daily/{start}/{end}")
    UA = "ATIP-research/1.0 (personal research; contact via project owner)"

    def entities(self, conn):
        cap = int(self.cfg.get("max_entities") or 100)
        try:
            rows = conn.execute("SELECT symbol FROM ai_scores WHERE date=(SELECT MAX(date) FROM ai_scores) "
                                "ORDER BY atip_score DESC LIMIT ?", (cap,)).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return super().entities(conn)[:cap]

    def _title(self, sym):
        m = self.cfg.get("map") or {}
        if sym in m:
            return m[sym]
        from data.companies import load_company_names
        name = (load_company_names() or {}).get(sym) or ""
        for suffix in (" LIMITED", " LTD", " LTD."):
            if name.upper().endswith(suffix):
                name = name[: -len(suffix)]
        return "_".join(w.capitalize() if w.isupper() else w for w in name.split()) if name else None

    def fetch(self, conn, as_of, entities):
        import requests
        end = as_of - timedelta(days=1)                  # the newest complete day
        start = end - timedelta(days=70)
        out = []
        for sym in entities:
            title = self._title(sym)
            if not title:
                continue
            try:
                r = requests.get(self.API.format(title=requests.utils.quote(title, safe=""),
                                                 start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")),
                                 timeout=20, headers={"User-Agent": self.UA})
            except Exception as e:
                log.debug(f"  wiki {sym}: {e}")
                continue
            if r.status_code != 200:
                continue
            items = r.json().get("items") or []
            series = [(datetime.strptime(i["timestamp"][:8], "%Y%m%d").date(), float(i.get("views") or 0)) for i in items]
            for k, (d, v) in enumerate(series):
                avail = datetime.combine(d + timedelta(days=1), datetime.min.time()).replace(hour=6)
                meta = {"title": title}
                out.append(Observation(sym, d, "views", v, avail, meta))
                z = _z(v, [x for _, x in series[max(0, k - 60):k]])
                if z is not None:
                    out.append(Observation(sym, d, "views_z60", z, avail, meta))
        return out
