"""
Tables and additive columns added in W28b (NS-05 news weighting, NS-06 announcement NLP).

Applied by db/schema.py _run_additive_migrations in a loop of its own.

    news_source_quality      NS-05  per-source measured weight (duplicate share, next-session reaction)
    news_symbol_score        NS-05  per-symbol decayed, novelty- and source-weighted news score
    corporate_announcement   NS-06  NSE corporate announcements, classified; document analysis in nlp_json
"""

W28B_TABLES = {
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
    "corporate_announcement": (
        """CREATE TABLE IF NOT EXISTS corporate_announcement (
            ann_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, company TEXT, broadcast_at TIMESTAMP,
            subject TEXT, detail TEXT, attachment_url TEXT, category TEXT, event_type TEXT, tone REAL,
            importance TEXT, confidence REAL, summary TEXT, classifier TEXT, nlp_json TEXT, fetched_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_corp_ann_symbol ON corporate_announcement(symbol, broadcast_at)",
        "CREATE INDEX IF NOT EXISTS idx_corp_ann_time ON corporate_announcement(broadcast_at)",
    ),
}

# NS-05 columns on news_articles. Older rows keep NULLs and are read as novelty 1.0,
# no duplicate, the feed's configured source weight and the category's half-life.
W28B_COLUMNS = {
    "news_articles": {"novelty": "REAL", "dup_of": "INTEGER", "source_weight": "REAL", "half_life_h": "REAL",
                      "published_at": "TIMESTAMP"},
}
