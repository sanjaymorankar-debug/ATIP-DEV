"""
Tables added in W36 (ML / strategy tooling), applied by db/schema.py _run_additive_migrations.

    ml_dl_benefit            ML-02  deep-learning benefit checks (network vs baselines, walk-forward)
    ml_rl_run                ML-04  reinforcement-learning research runs (result + Q-table)
    assistant_conversation   ML-16  chat conversations
    assistant_message        ML-16  question / answer / mode / tools / cost per turn
"""

W36_TABLES = {
    "ml_dl_benefit": (
        """CREATE TABLE IF NOT EXISTS ml_dl_benefit (
            check_id TEXT PRIMARY KEY, dataset_id TEXT, verdict TEXT NOT NULL, reason TEXT, result_json TEXT,
            created_at TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS idx_ml_dl_benefit_ds ON ml_dl_benefit(dataset_id, created_at)",
    ),
    "ml_rl_run": (
        """CREATE TABLE IF NOT EXISTS ml_rl_run (
            run_id TEXT PRIMARY KEY, verdict TEXT, result_json TEXT, q_table_json TEXT, created_at TIMESTAMP)""",
    ),
    "assistant_conversation": (
        """CREATE TABLE IF NOT EXISTS assistant_conversation (
            conversation_id TEXT PRIMARY KEY, title TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""",
    ),
    "assistant_message": (
        """CREATE TABLE IF NOT EXISTS assistant_message (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, asked_at TIMESTAMP, question TEXT,
            answer TEXT, mode TEXT, model TEXT, tools_json TEXT, cost_usd REAL)""",
        "CREATE INDEX IF NOT EXISTS idx_assistant_msg_conv ON assistant_message(conversation_id, id)",
    ),
}
