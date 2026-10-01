"""
Tables and additive columns added in W28 (signals, news intelligence, dashboards).

Kept in their own module, applied by db/schema.py _run_additive_migrations with one
loop of its own, so the W27 (data & scores) branch -- which edits the shared table
dict in schema.py -- and this branch merge without touching the same lines.

    news_ai_usage            NS-02  daily LLM call / token / cost ledger (the cost cap reads it)
    news_source_quality      NS-05  per-source measured weight (duplicate share, reaction)
    news_symbol_score        NS-05  per-symbol decayed, novelty- and source-weighted news score
    news_digest              NS-04  AI (or extractive) market news summary per session
    corporate_announcement   NS-06  NSE corporate announcements, classified
    intraday_scan_hit        SG-08  intraday scan hits from stored 15-min bars
    strategy_performance     DB-16  per-strategy forward-outcome and paper P&L metrics
"""

W28_TABLES = {
    "news_ai_usage": (
        """CREATE TABLE IF NOT EXISTS news_ai_usage (
            day DATE NOT NULL, purpose TEXT NOT NULL, model TEXT, calls INTEGER NOT NULL DEFAULT 0,
            items INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0, refusals INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP, PRIMARY KEY (day, purpose))""",
    ),
    "news_source_quality": (
        """CREATE TABLE IF NOT EXISTS news_source_quality (
            source TEXT PRIMARY KEY, articles INTEGER, duplicate_share REAL, symbol_share REAL,
            reaction_hit_rate REAL, reaction_n INTEGER, configured_weight REAL, weight REAL,
            detail_json TEXT, updated_at TIMESTAMP)""",
    ),
    "news_symbol_score": (
        """CREATE TABLE IF NOT EXISTS news_symbol_score (
            symbol TEXT NOT NULL, date DATE NOT NULL, score REAL, n_articles INTEGER, effective_weight REAL,
            mean_sentiment REAL, components_json TEXT, created_at TIMESTAMP,
            PRIMARY KEY (symbol, date))""",
    ),
    "news_digest": (
        """CREATE TABLE IF NOT EXISTS news_digest (
            digest_id TEXT PRIMARY KEY, date DATE NOT NULL, session TEXT NOT NULL, generated_at TIMESTAMP,
            method TEXT, model TEXT, headline TEXT, summary TEXT, bullets_json TEXT, themes_json TEXT,
            symbols_json TEXT, market_tone TEXT, articles_used INTEGER, article_ids_json TEXT,
            status TEXT NOT NULL DEFAULT 'OK', error TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_news_digest_date ON news_digest(date, generated_at)",
    ),
    "corporate_announcement": (
        """CREATE TABLE IF NOT EXISTS corporate_announcement (
            ann_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, company TEXT, broadcast_at TIMESTAMP,
            subject TEXT, detail TEXT, attachment_url TEXT, category TEXT, event_type TEXT, tone REAL,
            importance TEXT, confidence REAL, summary TEXT, classifier TEXT, nlp_json TEXT, fetched_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_corp_ann_symbol ON corporate_announcement(symbol, broadcast_at)",
        "CREATE INDEX IF NOT EXISTS idx_corp_ann_time ON corporate_announcement(broadcast_at)",
    ),
    "intraday_scan_hit": (
        """CREATE TABLE IF NOT EXISTS intraday_scan_hit (
            scan_date DATE NOT NULL, symbol TEXT NOT NULL, scan TEXT NOT NULL, bar_ts TIMESTAMP,
            direction TEXT, price REAL, strength REAL, detail_json TEXT, atip_score REAL, signal TEXT,
            first_seen_at TIMESTAMP, updated_at TIMESTAMP, alerted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (scan_date, symbol, scan))""",
        "CREATE INDEX IF NOT EXISTS idx_scan_hit_date ON intraday_scan_hit(scan_date, scan)",
    ),
    "strategy_performance": (
        """CREATE TABLE IF NOT EXISTS strategy_performance (
            strategy_id TEXT NOT NULL, version TEXT NOT NULL, as_of DATE NOT NULL, window_days INTEGER NOT NULL,
            metrics_json TEXT, created_at TIMESTAMP,
            PRIMARY KEY (strategy_id, version, as_of, window_days))""",
    ),
}

# Additive columns on news_articles (NS-02 / NS-03 / NS-05). Older rows keep NULLs and
# are read as: no event type, novelty 1.0, source weight from the feed config.
W28_COLUMNS = {
    "news_articles": {
        "event_type": "TEXT", "novelty": "REAL", "dup_of": "INTEGER", "source_weight": "REAL",
        "half_life_h": "REAL", "sentiment_lex": "REAL", "lex_confidence": "REAL", "model": "TEXT",
        "entities_json": "TEXT", "published_at": "TIMESTAMP",
    },
}
