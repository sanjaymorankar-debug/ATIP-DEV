-- =====================================================================
-- ATIP — atip_schema.mysql.sql   (MySQL 8 / MariaDB, e.g. Hostinger phpMyAdmin)
-- The same fresh database as atip_schema.sqlite.sql (master @ 6896bea),
-- converted by ATIP's own tools/export_mysql.py: 216 tables, all prefixed
-- `atip_` so they can't collide with a website's tables in a shared
-- database, plus seed rows (atip_weight_config 105,
-- atip_schema_migrations 5).
--
-- IMPORTANT: this is a reporting/dashboard COPY target, NOT the schema ATIP
-- runs on. Its tables are prefixed `atip_` and their types are inferred from
-- the data, so ATIP's own MySQL queries do not fit it. ATIP's runtime is
-- SQLite; its MySQL and PostgreSQL backends are experimental, and for a
-- database their queries DO fit use tools/sqlite_to_mysql.py (or init_db()
-- with the MySQL backend) rather than this file. To refresh this copy with
-- real data run tools/export_mysql.py on the production machine and import
-- its output instead.
--
-- phpMyAdmin: select the database, Import this file. It DROPs and
-- recreates ONLY the atip_* tables. Not carried over: the 16 append-only
-- triggers, foreign keys, partial indexes (see tools/export_mysql.py).
-- =====================================================================

-- ATIP export (20261003-135620) part schema
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;
SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';
SET AUTOCOMMIT=0;
START TRANSACTION;

DROP TABLE IF EXISTS `atip_accuracy_tracker`;
CREATE TABLE `atip_accuracy_tracker` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `pred_date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `signal` TEXT,
  `entry_price` DOUBLE,
  `price_5d` DOUBLE,
  `return_5d` DOUBLE,
  `correct_5d` BIGINT,
  `price_10d` DOUBLE,
  `return_10d` DOUBLE,
  `correct_10d` BIGINT,
  `price_20d` DOUBLE,
  `return_20d` DOUBLE,
  `correct_20d` BIGINT,
  `hit_target_1` BIGINT,
  `hit_stop_loss` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_accuracy_tracker_pred_date_symbol` (`pred_date`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ai_scores`;
CREATE TABLE `atip_ai_scores` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `vpi` DOUBLE,
  `spi` DOUBLE,
  `rri` DOUBLE,
  `mri` DOUBLE,
  `cri` DOUBLE,
  `msi` DOUBLE,
  `zpi` DOUBLE,
  `acs` DOUBLE,
  `tech_score` DOUBLE,
  `fund_score` DOUBLE,
  `inst_score` DOUBLE,
  `news_score` DOUBLE,
  `atip_score` DOUBLE,
  `atip_rank` BIGINT,
  `signal` TEXT,
  `confidence` DOUBLE,
  `beta_1y` DOUBLE,
  `tod_score` DOUBLE,
  `is_tod` BIGINT,
  `mh_score` DOUBLE,
  `regime` TEXT,
  `top_factor_1` TEXT,
  `top_factor_2` TEXT,
  `top_factor_3` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_scores_date_atip` (`date`, `atip_score`),
  UNIQUE KEY `uq_ai_scores_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ai_usage_log`;
CREATE TABLE `atip_ai_usage_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `day` DATE NOT NULL,
  `created_at` DATETIME(6),
  `purpose` TEXT,
  `model` TEXT,
  `input_tokens` BIGINT,
  `output_tokens` BIGINT,
  `cache_write_tokens` BIGINT,
  `cache_read_tokens` BIGINT,
  `cost_usd` DOUBLE,
  `ok` BIGINT,
  `error` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_ai_usage_day` (`day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_alert_log`;
CREATE TABLE `atip_alert_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME(6) NOT NULL,
  `category` TEXT NOT NULL,
  `severity` TEXT NOT NULL,
  `title` TEXT,
  `message` TEXT NOT NULL,
  `dedupe_key` VARCHAR(191),
  `telegram_sent` BIGINT NOT NULL,
  `telegram_error` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_alert_log_key` (`dedupe_key`, `created_at`),
  KEY `idx_alert_log_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_alt_dataset`;
CREATE TABLE `atip_alt_dataset` (
  `source_id` VARCHAR(191) NOT NULL,
  `name` TEXT,
  `description` TEXT,
  `entity` TEXT,
  `frequency` TEXT,
  `enabled` BIGINT,
  `last_run` DATETIME(6),
  `last_status` TEXT,
  `last_rows` BIGINT,
  `coverage` DOUBLE,
  `stale_days` BIGINT,
  `error` TEXT,
  `meta_json` TEXT,
  PRIMARY KEY (`source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_alt_observation`;
CREATE TABLE `atip_alt_observation` (
  `source_id` VARCHAR(191) NOT NULL,
  `entity` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `metric` VARCHAR(191) NOT NULL,
  `value` DOUBLE,
  `available_from` DATETIME(6),
  `meta_json` TEXT,
  PRIMARY KEY (`source_id`, `entity`, `date`, `metric`),
  KEY `idx_alt_obs` (`source_id`, `metric`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_asset_price_daily`;
CREATE TABLE `atip_asset_price_daily` (
  `asset_class` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `volume` DOUBLE,
  `currency` TEXT,
  `source` TEXT,
  PRIMARY KEY (`asset_class`, `symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_assistant_conversation`;
CREATE TABLE `atip_assistant_conversation` (
  `conversation_id` VARCHAR(191) NOT NULL,
  `title` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`conversation_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_assistant_message`;
CREATE TABLE `atip_assistant_message` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `conversation_id` VARCHAR(191) NOT NULL,
  `asked_at` DATETIME(6),
  `question` TEXT,
  `answer` TEXT,
  `mode` TEXT,
  `model` TEXT,
  `tools_json` TEXT,
  `cost_usd` DOUBLE,
  PRIMARY KEY (`id`),
  KEY `idx_assistant_msg_conv` (`conversation_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_audit_export`;
CREATE TABLE `atip_audit_export` (
  `export_id` VARCHAR(191) NOT NULL,
  `source` TEXT NOT NULL,
  `first_id` BIGINT,
  `last_id` BIGINT,
  `rows` BIGINT,
  `path` TEXT,
  `offbox_path` TEXT,
  `sha256` TEXT,
  `chain_ok` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`export_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_backtest_drawdown`;
CREATE TABLE `atip_backtest_drawdown` (
  `run_id` VARCHAR(191) NOT NULL,
  `seq` BIGINT NOT NULL,
  `peak_date` DATE,
  `trough_date` DATE,
  `recovery_date` DATE,
  `peak_equity` DOUBLE,
  `trough_equity` DOUBLE,
  `depth_pct` DOUBLE,
  `duration_sessions` BIGINT,
  `recovery_sessions` BIGINT,
  PRIMARY KEY (`run_id`, `seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_backtest_equity`;
CREATE TABLE `atip_backtest_equity` (
  `run_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `cash` DOUBLE,
  `positions_value` DOUBLE,
  `equity` DOUBLE,
  `exposure_pct` DOUBLE,
  `n_positions` BIGINT,
  `realized_cum` DOUBLE,
  `unrealized` DOUBLE,
  `daily_return` DOUBLE,
  `peak_equity` DOUBLE,
  `drawdown_pct` DOUBLE,
  PRIMARY KEY (`run_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_backtest_montecarlo`;
CREATE TABLE `atip_backtest_montecarlo` (
  `mc_id` VARCHAR(191) NOT NULL,
  `run_id` VARCHAR(191) NOT NULL,
  `method` TEXT NOT NULL,
  `n_sims` BIGINT,
  `seed` BIGINT,
  `params_json` TEXT,
  `results_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`mc_id`),
  KEY `idx_backtest_mc_run` (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_backtest_run`;
CREATE TABLE `atip_backtest_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `parent_run_id` VARCHAR(191),
  `kind` TEXT NOT NULL,
  `window_index` BIGINT,
  `strategy_id` VARCHAR(191) NOT NULL,
  `strategy_version` TEXT,
  `period_label` TEXT,
  `start_date` DATE,
  `end_date` DATE,
  `data_source` TEXT,
  `timeframe` TEXT,
  `initial_capital` DOUBLE,
  `params_json` TEXT,
  `config_json` TEXT NOT NULL,
  `config_hash` TEXT,
  `code_version` TEXT,
  `data_fingerprint_json` TEXT,
  `bias_report_json` TEXT,
  `metrics_json` TEXT,
  `summary_json` TEXT,
  `status` TEXT NOT NULL,
  `error` TEXT,
  `created_at` DATETIME(6),
  `started_at` DATETIME(6),
  `finished_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`run_id`),
  KEY `idx_backtest_run_parent` (`parent_run_id`),
  KEY `idx_backtest_run_strategy` (`strategy_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_backtest_trade`;
CREATE TABLE `atip_backtest_trade` (
  `run_id` VARCHAR(191) NOT NULL,
  `seq` BIGINT NOT NULL,
  `symbol` TEXT NOT NULL,
  `entry_date` DATE,
  `entry_price` DOUBLE,
  `entry_ref_price` DOUBLE,
  `qty` BIGINT,
  `exit_date` DATE,
  `exit_price` DOUBLE,
  `exit_ref_price` DOUBLE,
  `exit_reason` TEXT,
  `gross_pnl` DOUBLE,
  `costs` DOUBLE,
  `net_pnl` DOUBLE,
  `return_pct` DOUBLE,
  `holding_sessions` BIGINT,
  `entry_reason` TEXT,
  PRIMARY KEY (`run_id`, `seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_broker_health_check`;
CREATE TABLE `atip_broker_health_check` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `checked_at` DATETIME(6) NOT NULL,
  `overall` TEXT NOT NULL,
  `in_session` BIGINT,
  `checks_json` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_broker_health_at` (`checked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_broker_import_run`;
CREATE TABLE `atip_broker_import_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `owner_id` TEXT NOT NULL,
  `broker` TEXT,
  `kind` TEXT,
  `filename` TEXT,
  `rows_in` BIGINT,
  `added` BIGINT,
  `skipped` BIGINT,
  `errors` BIGINT,
  `created_at` DATETIME(6),
  `detail_json` TEXT,
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_bulk_deals`;
CREATE TABLE `atip_bulk_deals` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `net_value_cr` DOUBLE,
  `deal_count` BIGINT,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_bulk_symbol_date` (`symbol`, `date`),
  UNIQUE KEY `uq_bulk_deals_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_compliance_result`;
CREATE TABLE `atip_compliance_result` (
  `run_id` VARCHAR(191) NOT NULL,
  `check_id` VARCHAR(191) NOT NULL,
  `title` TEXT,
  `status` TEXT NOT NULL,
  `detail` TEXT,
  `evidence_json` TEXT,
  PRIMARY KEY (`run_id`, `check_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_compliance_run`;
CREATE TABLE `atip_compliance_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `at` DATETIME(6) NOT NULL,
  `trigger` TEXT,
  `passed` BIGINT,
  `warned` BIGINT,
  `failed` BIGINT,
  `summary` TEXT,
  PRIMARY KEY (`run_id`),
  KEY `idx_compliance_run_at` (`at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_corporate_actions`;
CREATE TABLE `atip_corporate_actions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `ex_date` DATE NOT NULL,
  `subject` VARCHAR(191) NOT NULL,
  `kind` TEXT,
  `factor` DOUBLE,
  `status` TEXT NOT NULL,
  `price_factor` DOUBLE,
  `price_rows` BIGINT,
  `volume_rows` BIGINT,
  `note` TEXT,
  `created_at` DATETIME(6),
  `reconciled_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_ca_symbol` (`symbol`, `ex_date`),
  UNIQUE KEY `uq_corporate_actions_symbol_ex_date_subject` (`symbol`, `ex_date`, `subject`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_corporate_announcement`;
CREATE TABLE `atip_corporate_announcement` (
  `ann_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `company` TEXT,
  `broadcast_at` DATETIME(6),
  `subject` TEXT,
  `detail` TEXT,
  `attachment_url` TEXT,
  `category` TEXT,
  `event_type` TEXT,
  `tone` DOUBLE,
  `importance` TEXT,
  `confidence` DOUBLE,
  `summary` TEXT,
  `classifier` TEXT,
  `nlp_json` TEXT,
  `fetched_at` DATETIME(6),
  PRIMARY KEY (`ann_id`),
  KEY `idx_corp_ann_time` (`broadcast_at`),
  KEY `idx_corp_ann_symbol` (`symbol`, `broadcast_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_data_quality`;
CREATE TABLE `atip_data_quality` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `check_name` VARCHAR(191) NOT NULL,
  `severity` TEXT NOT NULL,
  `failed` BIGINT,
  `checked` BIGINT,
  `score` DOUBLE,
  `detail` TEXT,
  `run_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_data_quality_date_check_name` (`date`, `check_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_derivatives_instrument`;
CREATE TABLE `atip_derivatives_instrument` (
  `instrument_id` VARCHAR(191) NOT NULL,
  `underlying` TEXT NOT NULL,
  `instrument_type` TEXT NOT NULL,
  `expiry` DATE,
  `strike` DOUBLE,
  `option_type` TEXT,
  `lot_size` BIGINT,
  `exchange` TEXT,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`instrument_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_derivatives_quote`;
CREATE TABLE `atip_derivatives_quote` (
  `instrument_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `settle` DOUBLE,
  `volume` BIGINT,
  `open_interest` BIGINT,
  `oi_change` BIGINT,
  `underlying_close` DOUBLE,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`instrument_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_alert_rule`;
CREATE TABLE `atip_enterprise_alert_rule` (
  `rule_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `user_id` TEXT NOT NULL,
  `name` TEXT,
  `symbol` TEXT,
  `feature` TEXT,
  `op` TEXT,
  `value` DOUBLE,
  `status` TEXT,
  `last_value` DOUBLE,
  `last_triggered_at` DATETIME(6),
  `created_at` DATETIME(6),
  PRIMARY KEY (`rule_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_api_key`;
CREATE TABLE `atip_enterprise_api_key` (
  `key_id` VARCHAR(191) NOT NULL,
  `user_id` TEXT NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `name` TEXT,
  `key_hash` VARCHAR(191) NOT NULL,
  `scopes_json` TEXT,
  `created_at` DATETIME(6),
  `expires_at` DATETIME(6),
  `last_used_at` DATETIME(6),
  `revoked_at` DATETIME(6),
  `rate_limit_per_minute` BIGINT,
  `daily_quota` BIGINT,
  PRIMARY KEY (`key_id`),
  UNIQUE KEY `uq_enterprise_api_key_key_hash` (`key_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_api_usage`;
CREATE TABLE `atip_enterprise_api_usage` (
  `key_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `date` DATE NOT NULL,
  `calls` BIGINT NOT NULL,
  PRIMARY KEY (`key_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_audit`;
CREATE TABLE `atip_enterprise_audit` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `at` DATETIME(6),
  `tenant_id` VARCHAR(191),
  `user_id` TEXT,
  `actor` TEXT,
  `action` TEXT NOT NULL,
  `resource` TEXT,
  `method` TEXT,
  `path` TEXT,
  `status_code` BIGINT,
  `ip` TEXT,
  `details_json` TEXT,
  `prev_hash` TEXT,
  `row_hash` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_ent_audit_at` (`at`),
  KEY `idx_ent_audit_tenant` (`tenant_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_consent`;
CREATE TABLE `atip_enterprise_consent` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `tenant_id` TEXT,
  `user_id` VARCHAR(191) NOT NULL,
  `document` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `accepted_at` DATETIME(6),
  `ip` TEXT,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_enterprise_consent_user_id_document_version` (`user_id`, `document`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_email_token`;
CREATE TABLE `atip_enterprise_email_token` (
  `token_hash` VARCHAR(191) NOT NULL,
  `user_id` TEXT NOT NULL,
  `purpose` TEXT NOT NULL,
  `email` TEXT,
  `expires_at` DATETIME(6),
  `used_at` DATETIME(6),
  `created_at` DATETIME(6),
  PRIMARY KEY (`token_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_invoice`;
CREATE TABLE `atip_enterprise_invoice` (
  `invoice_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `period_start` DATE,
  `period_end` DATE,
  `plan_id` TEXT,
  `amount` DOUBLE,
  `currency` TEXT,
  `status` TEXT NOT NULL,
  `lines_json` TEXT,
  `created_at` DATETIME(6),
  `due_date` DATE,
  `paid_at` DATETIME(6),
  `payment_id` TEXT,
  `finalized_at` DATETIME(6),
  PRIMARY KEY (`invoice_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_mfa_recovery`;
CREATE TABLE `atip_enterprise_mfa_recovery` (
  `code_hash` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `created_at` DATETIME(6),
  `used_at` DATETIME(6),
  PRIMARY KEY (`code_hash`),
  KEY `idx_mfa_recovery_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_notification`;
CREATE TABLE `atip_enterprise_notification` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `tenant_id` TEXT,
  `user_id` VARCHAR(191) NOT NULL,
  `category` TEXT,
  `severity` TEXT,
  `title` TEXT,
  `body` TEXT,
  `created_at` DATETIME(6),
  `read_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_ent_notif_user` (`user_id`, `read_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_notification_delivery`;
CREATE TABLE `atip_enterprise_notification_delivery` (
  `delivery_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT,
  `notification_id` BIGINT,
  `user_id` VARCHAR(191) NOT NULL,
  `channel` TEXT NOT NULL,
  `destination_masked` TEXT,
  `subject` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `mode` TEXT,
  `error` TEXT,
  `created_at` DATETIME(6),
  `sent_at` DATETIME(6),
  PRIMARY KEY (`delivery_id`),
  KEY `idx_ent_ndel_user` (`user_id`, `created_at`),
  KEY `idx_ent_ndel_status` (`status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_notification_pref`;
CREATE TABLE `atip_enterprise_notification_pref` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `category` VARCHAR(191) NOT NULL,
  `channels_json` TEXT,
  `mode` TEXT NOT NULL,
  `quiet_start` TEXT,
  `quiet_end` TEXT,
  `unsubscribed` BIGINT NOT NULL,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `user_id`, `category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_onboarding`;
CREATE TABLE `atip_enterprise_onboarding` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `steps_json` TEXT,
  `completed_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_password_reset`;
CREATE TABLE `atip_enterprise_password_reset` (
  `token_hash` VARCHAR(191) NOT NULL,
  `user_id` TEXT NOT NULL,
  `expires_at` DATETIME(6),
  `created_by` TEXT,
  `created_at` DATETIME(6),
  `used_at` DATETIME(6),
  PRIMARY KEY (`token_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_payment`;
CREATE TABLE `atip_enterprise_payment` (
  `payment_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `invoice_id` TEXT,
  `provider` TEXT NOT NULL,
  `amount` DOUBLE,
  `currency` TEXT,
  `status` TEXT NOT NULL,
  `provider_ref` TEXT,
  `idempotency_key` VARCHAR(191),
  `error` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`payment_id`),
  KEY `idx_ent_payment_tenant` (`tenant_id`, `created_at`),
  UNIQUE KEY `uq_enterprise_payment_idempotency_key` (`idempotency_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_permission`;
CREATE TABLE `atip_enterprise_permission` (
  `permission` VARCHAR(191) NOT NULL,
  `description` TEXT,
  PRIMARY KEY (`permission`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_plan`;
CREATE TABLE `atip_enterprise_plan` (
  `plan_id` VARCHAR(191) NOT NULL,
  `name` TEXT,
  `price_month` DOUBLE,
  `currency` TEXT,
  `limits_json` TEXT,
  `features_json` TEXT,
  `status` TEXT,
  PRIMARY KEY (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_privacy_request`;
CREATE TABLE `atip_enterprise_privacy_request` (
  `request_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `user_id` TEXT NOT NULL,
  `kind` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `reason` TEXT,
  `requested_at` DATETIME(6),
  `decided_by` TEXT,
  `decided_at` DATETIME(6),
  `completed_at` DATETIME(6),
  `result_json` TEXT,
  PRIMARY KEY (`request_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_refresh_token`;
CREATE TABLE `atip_enterprise_refresh_token` (
  `token_hash` VARCHAR(191) NOT NULL,
  `family_id` VARCHAR(191) NOT NULL,
  `user_id` TEXT NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `expires_at` DATETIME(6),
  `used_at` DATETIME(6),
  `revoked_at` DATETIME(6),
  `replaced_by` TEXT,
  `ip` TEXT,
  PRIMARY KEY (`token_hash`),
  KEY `idx_ent_refresh_family` (`family_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_report`;
CREATE TABLE `atip_enterprise_report` (
  `report_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `user_id` TEXT NOT NULL,
  `name` TEXT,
  `kind` TEXT,
  `params_json` TEXT,
  `shared` BIGINT NOT NULL,
  `created_at` DATETIME(6),
  `schedule` TEXT,
  `formats_json` TEXT,
  `last_run_at` DATETIME(6),
  PRIMARY KEY (`report_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_report_output`;
CREATE TABLE `atip_enterprise_report_output` (
  `output_id` VARCHAR(191) NOT NULL,
  `report_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `user_id` TEXT,
  `format` TEXT NOT NULL,
  `content` TEXT,
  `rows` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`output_id`),
  KEY `idx_ent_rout_report` (`report_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_risk_profile`;
CREATE TABLE `atip_enterprise_risk_profile` (
  `scope` VARCHAR(191) NOT NULL,
  `scope_id` VARCHAR(191) NOT NULL,
  `profile_json` TEXT,
  `updated_at` DATETIME(6),
  `updated_by` TEXT,
  PRIMARY KEY (`scope`, `scope_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_role`;
CREATE TABLE `atip_enterprise_role` (
  `role` VARCHAR(191) NOT NULL,
  `description` TEXT,
  `builtin` BIGINT NOT NULL,
  `created_at` DATETIME(6),
  PRIMARY KEY (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_role_permission`;
CREATE TABLE `atip_enterprise_role_permission` (
  `role` VARCHAR(191) NOT NULL,
  `permission` VARCHAR(191) NOT NULL,
  PRIMARY KEY (`role`, `permission`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_session`;
CREATE TABLE `atip_enterprise_session` (
  `token_hash` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `expires_at` DATETIME(6),
  `last_seen_at` DATETIME(6),
  `ip` TEXT,
  `user_agent` TEXT,
  `revoked_at` DATETIME(6),
  PRIMARY KEY (`token_hash`),
  KEY `idx_ent_session_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_subscription`;
CREATE TABLE `atip_enterprise_subscription` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `plan_id` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `started_at` DATETIME(6),
  `current_period_end` DATETIME(6),
  `cancel_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `dunning_state` TEXT,
  `grace_until` DATETIME(6),
  `payment_provider` TEXT,
  PRIMARY KEY (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_tenant`;
CREATE TABLE `atip_enterprise_tenant` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `settings_json` TEXT,
  `limits_json` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_usage`;
CREATE TABLE `atip_enterprise_usage` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `metric` VARCHAR(191) NOT NULL,
  `value` DOUBLE,
  PRIMARY KEY (`tenant_id`, `date`, `metric`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_user`;
CREATE TABLE `atip_enterprise_user` (
  `user_id` VARCHAR(191) NOT NULL,
  `username` VARCHAR(191) NOT NULL,
  `email` TEXT,
  `display_name` TEXT,
  `password_hash` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `failed_logins` BIGINT NOT NULL,
  `locked_until` DATETIME(6),
  `must_change_password` BIGINT NOT NULL,
  `preferences_json` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `last_login_at` DATETIME(6),
  `created_by` TEXT,
  `mfa_enabled` BIGINT NOT NULL,
  `mfa_secret_enc` TEXT,
  `mfa_pending_enc` TEXT,
  `email_verified_at` DATETIME(6),
  `deleted_at` DATETIME(6),
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uq_enterprise_user_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_user_role`;
CREATE TABLE `atip_enterprise_user_role` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `role` VARCHAR(191) NOT NULL,
  `granted_by` TEXT,
  `granted_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `user_id`, `role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_vault_credential`;
CREATE TABLE `atip_enterprise_vault_credential` (
  `credential_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `broker` VARCHAR(191) NOT NULL,
  `label` VARCHAR(191),
  `secret_enc` TEXT NOT NULL,
  `field_names_json` TEXT,
  `key_id` TEXT,
  `status` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `rotated_at` DATETIME(6),
  `last_accessed_at` DATETIME(6),
  `access_count` BIGINT NOT NULL,
  `expires_at` DATETIME(6),
  `last_verified_at` DATETIME(6),
  `verify_status` TEXT,
  `verify_detail` TEXT,
  PRIMARY KEY (`credential_id`),
  UNIQUE KEY `uq_enterprise_vault_credential_tenant_id_user_id_broker_label` (`tenant_id`, `user_id`, `broker`, `label`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_enterprise_watchlist`;
CREATE TABLE `atip_enterprise_watchlist` (
  `watchlist_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `user_id` TEXT NOT NULL,
  `name` TEXT,
  `symbols_json` TEXT,
  `shared` BIGINT NOT NULL,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`watchlist_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_event_study`;
CREATE TABLE `atip_event_study` (
  `study_id` VARCHAR(191) NOT NULL,
  `event_type` TEXT NOT NULL,
  `period` TEXT,
  `params_json` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`study_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_exec_algo_parent`;
CREATE TABLE `atip_exec_algo_parent` (
  `parent_id` VARCHAR(191) NOT NULL,
  `risk_decision_id` VARCHAR(191) NOT NULL,
  `intent_id` TEXT,
  `decision_id` TEXT,
  `strategy_id` TEXT,
  `strategy_version` TEXT,
  `symbol` TEXT NOT NULL,
  `side` TEXT NOT NULL,
  `total_qty` BIGINT NOT NULL,
  `algo` TEXT NOT NULL,
  `params_json` TEXT,
  `start_at` DATETIME(6),
  `end_at` DATETIME(6),
  `status` VARCHAR(191) NOT NULL,
  `filled_qty` BIGINT NOT NULL,
  `avg_price` DOUBLE,
  `child_count` BIGINT NOT NULL,
  `reference_price` DOUBLE,
  `impact_estimate_json` TEXT,
  `mode` TEXT,
  `reason` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`parent_id`),
  KEY `idx_algo_parent_status` (`status`, `start_at`),
  UNIQUE KEY `uq_exec_algo_parent_risk_decision_id` (`risk_decision_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_execution_impact_calibration`;
CREATE TABLE `atip_execution_impact_calibration` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME(6),
  `n_fills` BIGINT,
  `y` DOUBLE,
  `residual_bps` DOUBLE,
  `adopted` BIGINT,
  `detail_json` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_fii_dii_market`;
CREATE TABLE `atip_fii_dii_market` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `fii_buy_cr` DOUBLE,
  `fii_sell_cr` DOUBLE,
  `fii_net_cr` DOUBLE,
  `dii_buy_cr` DOUBLE,
  `dii_sell_cr` DOUBLE,
  `dii_net_cr` DOUBLE,
  `fii_5d_avg` DOUBLE,
  `dii_5d_avg` DOUBLE,
  `pcr` DOUBLE,
  `mwpl_pct` DOUBLE,
  `adv_decline` DOUBLE,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fii_dii_market_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_fo_contract_daily`;
CREATE TABLE `atip_fo_contract_daily` (
  `date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `instrument` VARCHAR(191) NOT NULL,
  `expiry` DATE NOT NULL,
  `strike` DOUBLE NOT NULL,
  `option_type` VARCHAR(191) NOT NULL,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `settle` DOUBLE,
  `prev_close` DOUBLE,
  `oi` DOUBLE,
  `oi_chg` DOUBLE,
  `volume` DOUBLE,
  `value` DOUBLE,
  `underlying` DOUBLE,
  `lot_size` BIGINT,
  `iv` DOUBLE,
  PRIMARY KEY (`date`, `symbol`, `instrument`, `expiry`, `strike`, `option_type`),
  KEY `idx_focd_sym` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_fo_underlying_daily`;
CREATE TABLE `atip_fo_underlying_daily` (
  `date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `kind` TEXT NOT NULL,
  `underlying_price` DOUBLE,
  `fut_close` DOUBLE,
  `fut_oi` DOUBLE,
  `fut_oi_chg` DOUBLE,
  `fut_volume` DOUBLE,
  `call_oi` DOUBLE,
  `put_oi` DOUBLE,
  `call_oi_chg` DOUBLE,
  `put_oi_chg` DOUBLE,
  `call_volume` DOUBLE,
  `put_volume` DOUBLE,
  `pcr_oi` DOUBLE,
  `pcr_volume` DOUBLE,
  `max_pain` DOUBLE,
  `near_expiry` DATE,
  `created_at` DATETIME(6),
  `atm_iv` DOUBLE,
  `iv_call_atm` DOUBLE,
  `iv_put_atm` DOUBLE,
  `iv_skew` DOUBLE,
  `iv_expiry` DATE,
  `iv_dte` BIGINT,
  `lot_size` BIGINT,
  PRIMARY KEY (`date`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_formula_registry`;
CREATE TABLE `atip_formula_registry` (
  `weights_hash` VARCHAR(191) NOT NULL,
  `weights_json` TEXT NOT NULL,
  `notes_json` TEXT,
  `code_version` TEXT,
  `first_seen_at` DATETIME(6),
  PRIMARY KEY (`weights_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_fundamental_data`;
CREATE TABLE `atip_fundamental_data` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `quarter` VARCHAR(191) NOT NULL,
  `report_date` DATE,
  `roe` DOUBLE,
  `roce` DOUBLE,
  `net_margin` DOUBLE,
  `operating_margin` DOUBLE,
  `roa` DOUBLE,
  `eps_ttm` DOUBLE,
  `eps_growth_yoy` DOUBLE,
  `revenue_cr` DOUBLE,
  `revenue_growth_yoy` DOUBLE,
  `profit_cr` DOUBLE,
  `profit_growth_yoy` DOUBLE,
  `qoq_revenue_chg` DOUBLE,
  `qoq_profit_chg` DOUBLE,
  `debt_equity` DOUBLE,
  `current_ratio` DOUBLE,
  `interest_coverage` DOUBLE,
  `fcf_cr` DOUBLE,
  `cash_cr` DOUBLE,
  `pe_ratio` DOUBLE,
  `pb_ratio` DOUBLE,
  `ev_ebitda` DOUBLE,
  `peg_ratio` DOUBLE,
  `dividend_yield` DOUBLE,
  `book_value_ps` DOUBLE,
  `promoter_hold` DOUBLE,
  `promoter_pledge` DOUBLE,
  `inst_hold` DOUBLE,
  `fundamental_score` DOUBLE,
  `source` TEXT,
  `created_at` DATETIME(6),
  `period_end` DATE,
  `available_from` DATETIME(6),
  `nature` TEXT,
  `eps_q` DOUBLE,
  `spi_score` DOUBLE,
  `score_inputs` TEXT,
  `mf_hold` DOUBLE,
  `fpi_hold` DOUBLE,
  `shares_out` DOUBLE,
  `equity_cr` DOUBLE,
  `debt_cr` DOUBLE,
  `profit_fy_cr` DOUBLE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fundamental_data_symbol_quarter` (`symbol`, `quarter`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_fundamental_filing`;
CREATE TABLE `atip_fundamental_filing` (
  `xbrl_url` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `period_end` DATE NOT NULL,
  `nature` TEXT NOT NULL,
  `audited` TEXT,
  `broadcast_at` DATETIME(6),
  `facts_json` TEXT,
  `status` TEXT NOT NULL,
  `error` TEXT,
  `fetched_at` DATETIME(6),
  PRIMARY KEY (`xbrl_url`),
  KEY `idx_ff_symbol_period` (`symbol`, `period_end`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_global_market_history`;
CREATE TABLE `atip_global_market_history` (
  `series` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `close` DOUBLE,
  `source` TEXT,
  PRIMARY KEY (`series`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_global_markets`;
CREATE TABLE `atip_global_markets` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `time` VARCHAR(191),
  `sp500` DOUBLE,
  `sp500_chg` DOUBLE,
  `dow` DOUBLE,
  `dow_chg` DOUBLE,
  `nasdaq` DOUBLE,
  `nasdaq_chg` DOUBLE,
  `nikkei` DOUBLE,
  `nikkei_chg` DOUBLE,
  `hangseng` DOUBLE,
  `hangseng_chg` DOUBLE,
  `ftse100` DOUBLE,
  `ftse100_chg` DOUBLE,
  `dax` DOUBLE,
  `dax_chg` DOUBLE,
  `crude_wti` DOUBLE,
  `crude_wti_chg` DOUBLE,
  `crude_brent` DOUBLE,
  `crude_brent_chg` DOUBLE,
  `gold` DOUBLE,
  `gold_chg` DOUBLE,
  `silver` DOUBLE,
  `silver_chg` DOUBLE,
  `usd_inr` DOUBLE,
  `usd_inr_chg` DOUBLE,
  `usd_index` DOUBLE,
  `usd_index_chg` DOUBLE,
  `us_10y` DOUBLE,
  `us_10y_chg` DOUBLE,
  `global_score` DOUBLE,
  `us_score` DOUBLE,
  `asia_score` DOUBLE,
  `commodity_score` DOUBLE,
  `global_sentiment` TEXT,
  `created_at` DATETIME(6),
  `us_3m` DOUBLE,
  `us_3m_chg` DOUBLE,
  `us_5y` DOUBLE,
  `us_5y_chg` DOUBLE,
  `us_30y` DOUBLE,
  `us_30y_chg` DOUBLE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_global_markets_date_time` (`date`, `time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_index_levels`;
CREATE TABLE `atip_index_levels` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `time` VARCHAR(191) NOT NULL,
  `nifty50` DOUBLE,
  `nifty50_chg` DOUBLE,
  `banknifty` DOUBLE,
  `banknifty_chg` DOUBLE,
  `midcap150` DOUBLE,
  `midcap150_chg` DOUBLE,
  `smallcap250` DOUBLE,
  `smallcap250_chg` DOUBLE,
  `nifty_it` DOUBLE,
  `nifty_it_chg` DOUBLE,
  `nifty_auto` DOUBLE,
  `nifty_auto_chg` DOUBLE,
  `nifty_fmcg` DOUBLE,
  `nifty_fmcg_chg` DOUBLE,
  `nifty_metal` DOUBLE,
  `nifty_metal_chg` DOUBLE,
  `nifty_realty` DOUBLE,
  `nifty_realty_chg` DOUBLE,
  `nifty_psubank` DOUBLE,
  `nifty_psubank_chg` DOUBLE,
  `nifty_energy` DOUBLE,
  `nifty_energy_chg` DOUBLE,
  `nifty_pharma` DOUBLE,
  `nifty_pharma_chg` DOUBLE,
  `india_vix` DOUBLE,
  `india_vix_chg` DOUBLE,
  `gift_nifty` DOUBLE,
  `gift_nifty_chg` DOUBLE,
  `overall_sentiment` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_index_levels_date_time` (`date`, `time`),
  KEY `idx_index_datetime` (`date`, `time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_insider_trade`;
CREATE TABLE `atip_insider_trade` (
  `disclosure_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `person` TEXT,
  `person_category` TEXT,
  `txn_type` TEXT,
  `security_type` TEXT,
  `qty` DOUBLE,
  `value_rs` DOUBLE,
  `mode` TEXT,
  `txn_from` DATE,
  `txn_to` DATE,
  `disclosed_at` DATETIME(6),
  `post_pct` DOUBLE,
  `fetched_at` DATETIME(6),
  PRIMARY KEY (`disclosure_id`),
  KEY `idx_insider_symbol` (`symbol`, `disclosed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_institutional_data`;
CREATE TABLE `atip_institutional_data` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `fii_net_cr` DOUBLE,
  `dii_net_cr` DOUBLE,
  `mf_net_cr` DOUBLE,
  `promoter_buy` BIGINT,
  `promoter_sell` BIGINT,
  `delivery_pct` DOUBLE,
  `inst_score` DOUBLE,
  `created_at` DATETIME(6),
  `ins_components` TEXT,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_institutional_data_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_intraday_bars`;
CREATE TABLE `atip_intraday_bars` (
  `symbol` VARCHAR(191) NOT NULL,
  `ts` DATETIME(6) NOT NULL,
  `interval_min` BIGINT NOT NULL,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `volume` BIGINT,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`symbol`, `interval_min`, `ts`),
  KEY `idx_intraday_bars_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_intraday_scan_hit`;
CREATE TABLE `atip_intraday_scan_hit` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `run_id` TEXT NOT NULL,
  `run_at` DATETIME(6),
  `session` DATE,
  `scan` VARCHAR(191),
  `symbol` TEXT,
  `price` DOUBLE,
  `score` DOUBLE,
  `details_json` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_scan_hit_session` (`session`, `scan`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_investor_profile`;
CREATE TABLE `atip_investor_profile` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `profile_id` TEXT NOT NULL,
  `version` BIGINT,
  `band` TEXT,
  `risk_score` DOUBLE,
  `mode` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `owner_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_investor_profile_version`;
CREATE TABLE `atip_investor_profile_version` (
  `profile_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `version` BIGINT NOT NULL,
  `questionnaire_version` TEXT,
  `methodology_version` TEXT,
  `answers_json` TEXT,
  `answers_hash` TEXT,
  `result_json` TEXT,
  `band` TEXT,
  `risk_score` DOUBLE,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  PRIMARY KEY (`profile_id`),
  UNIQUE KEY `uq_investor_profile_version_tenant_id_owner_id_version` (`tenant_id`, `owner_id`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_job_recovery`;
CREATE TABLE `atip_job_recovery` (
  `day` VARCHAR(191) NOT NULL,
  `step` VARCHAR(191) NOT NULL,
  `attempts` BIGINT NOT NULL,
  `last_at` DATETIME(6),
  `last_result` TEXT,
  `problems` TEXT,
  PRIMARY KEY (`day`, `step`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_lake_partition`;
CREATE TABLE `atip_lake_partition` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `dataset` VARCHAR(191) NOT NULL,
  `partition_date` DATE NOT NULL,
  `version` BIGINT NOT NULL,
  `path` TEXT NOT NULL,
  `format` TEXT,
  `rows` BIGINT,
  `columns_json` TEXT,
  `sha256` TEXT,
  `bytes` BIGINT,
  `source` TEXT,
  `knowledge_time` DATETIME(6),
  `written_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_lake_ds_date` (`dataset`, `partition_date`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_latency_rollup`;
CREATE TABLE `atip_latency_rollup` (
  `stage` VARCHAR(191) NOT NULL,
  `minute` DATETIME(6) NOT NULL,
  `n` BIGINT,
  `p50_ms` DOUBLE,
  `p95_ms` DOUBLE,
  `p99_ms` DOUBLE,
  `max_ms` DOUBLE,
  `mean_ms` DOUBLE,
  PRIMARY KEY (`stage`, `minute`),
  KEY `idx_latency_minute` (`minute`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_live_feed_status`;
CREATE TABLE `atip_live_feed_status` (
  `feed` VARCHAR(191) NOT NULL,
  `mode` TEXT,
  `subscribed` BIGINT,
  `ticks` BIGINT,
  `last_tick_at` DATETIME(6),
  `last_flush_at` DATETIME(6),
  `detail` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`feed`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_live_pnl_snapshot`;
CREATE TABLE `atip_live_pnl_snapshot` (
  `ts` DATETIME(6) NOT NULL,
  `book` VARCHAR(191) NOT NULL,
  `value` DOUBLE,
  `day_pnl` DOUBLE,
  `unrealized` DOUBLE,
  `positions` BIGINT,
  PRIMARY KEY (`ts`, `book`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_live_quotes`;
CREATE TABLE `atip_live_quotes` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `ltp` DOUBLE,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `prev_close` DOUBLE,
  `volume` BIGINT,
  `chg_pct` DOUBLE,
  `timestamp` VARCHAR(191),
  `source` TEXT,
  `buy_qty` DOUBLE,
  `sell_qty` DOUBLE,
  PRIMARY KEY (`id`),
  KEY `idx_lq_timestamp` (`timestamp`),
  KEY `idx_lq_symbol` (`symbol`, `timestamp`),
  UNIQUE KEY `uq_live_quotes_symbol_timestamp` (`symbol`, `timestamp`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_live_ticks`;
CREATE TABLE `atip_live_ticks` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191),
  `security_id` TEXT,
  `ltp` DOUBLE,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `volume` BIGINT,
  `timestamp` TEXT,
  `received_at` VARCHAR(191),
  PRIMARY KEY (`id`),
  KEY `idx_lt_symbol` (`symbol`, `received_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_macro_calendar`;
CREATE TABLE `atip_macro_calendar` (
  `event_date` DATE NOT NULL,
  `event` VARCHAR(191) NOT NULL,
  `country` TEXT,
  `importance` TEXT,
  `series_id` TEXT,
  `source` TEXT,
  `note` TEXT,
  PRIMARY KEY (`event_date`, `event`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_macro_observation`;
CREATE TABLE `atip_macro_observation` (
  `series_id` VARCHAR(191) NOT NULL,
  `period` DATE NOT NULL,
  `value` DOUBLE,
  `available_from` DATE NOT NULL,
  `first_seen` DATETIME(6),
  `revised` BIGINT NOT NULL,
  PRIMARY KEY (`series_id`, `period`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_macro_series`;
CREATE TABLE `atip_macro_series` (
  `series_id` VARCHAR(191) NOT NULL,
  `name` TEXT,
  `country` TEXT,
  `source` TEXT,
  `source_code` TEXT,
  `frequency` TEXT,
  `unit` TEXT,
  `release_lag_days` BIGINT,
  `transform` TEXT,
  `last_fetch` DATETIME(6),
  `last_period` DATE,
  `status` TEXT,
  `error` TEXT,
  PRIMARY KEY (`series_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_market_event`;
CREATE TABLE `atip_market_event` (
  `event_id` VARCHAR(191) NOT NULL,
  `source` VARCHAR(191) NOT NULL,
  `source_id` VARCHAR(191),
  `symbol` VARCHAR(191),
  `event_type` TEXT NOT NULL,
  `category` TEXT,
  `event_date` DATE,
  `known_at` DATE NOT NULL,
  `direction` TEXT,
  `value` DOUBLE,
  `payload_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`event_id`),
  KEY `idx_market_event_sym` (`symbol`, `known_at`),
  UNIQUE KEY `uq_market_event_source_source_id` (`source`, `source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_market_health`;
CREATE TABLE `atip_market_health` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `mh_score` DOUBLE,
  `regime` TEXT,
  `nifty_trend` DOUBLE,
  `banknifty` DOUBLE,
  `breadth` DOUBLE,
  `vix_score` DOUBLE,
  `fii_score` DOUBLE,
  `dii_score` DOUBLE,
  `global_score` DOUBLE,
  `sector_score` DOUBLE,
  `adv_decline` DOUBLE,
  `nifty_close` DOUBLE,
  `vix_level` DOUBLE,
  `created_at` DATETIME(6),
  `advances` BIGINT,
  `declines` BIGINT,
  `pct_advancing` DOUBLE,
  `new_highs` BIGINT,
  `new_lows` BIGINT,
  `breadth_universe` BIGINT,
  `mh_coverage` DOUBLE,
  `mh_inputs` TEXT,
  `backfilled` BIGINT,
  `portfolio_health` DOUBLE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_market_health_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_mf_nav`;
CREATE TABLE `atip_mf_nav` (
  `scheme_code` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `nav` DOUBLE,
  `scheme_name` TEXT,
  `isin_growth` TEXT,
  `isin_reinvest` TEXT,
  `amc` TEXT,
  `category` TEXT,
  PRIMARY KEY (`scheme_code`, `date`),
  KEY `idx_mfnav_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_microstructure_feature`;
CREATE TABLE `atip_microstructure_feature` (
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `feature` VARCHAR(191) NOT NULL,
  `value` DOUBLE,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`symbol`, `date`, `feature`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_anomaly`;
CREATE TABLE `atip_ml_anomaly` (
  `as_of` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `kind` TEXT,
  `score` DOUBLE,
  `detail_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`as_of`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_cluster`;
CREATE TABLE `atip_ml_cluster` (
  `run_id` VARCHAR(191) NOT NULL,
  `as_of` DATE,
  `symbol` VARCHAR(191) NOT NULL,
  `cluster` BIGINT,
  PRIMARY KEY (`run_id`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_cluster_run`;
CREATE TABLE `atip_ml_cluster_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `as_of` DATE,
  `method` TEXT,
  `k` BIGINT,
  `summary_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_dataset`;
CREATE TABLE `atip_ml_dataset` (
  `dataset_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `version` TEXT NOT NULL,
  `spec_json` TEXT NOT NULL,
  `spec_hash` TEXT NOT NULL,
  `feature_set` TEXT NOT NULL,
  `label_json` TEXT NOT NULL,
  `start_date` DATE,
  `end_date` DATE,
  `universe_json` TEXT,
  `frequency` TEXT,
  `sampling_json` TEXT,
  `source` TEXT,
  `status` TEXT,
  `summary_json` TEXT,
  `snapshot_path` TEXT,
  `snapshot_hash` TEXT,
  `created_at` DATETIME(6),
  `built_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`dataset_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_dl_benefit`;
CREATE TABLE `atip_ml_dl_benefit` (
  `check_id` VARCHAR(191) NOT NULL,
  `dataset_id` VARCHAR(191),
  `verdict` TEXT NOT NULL,
  `reason` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`check_id`),
  KEY `idx_ml_dl_benefit_ds` (`dataset_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_feature`;
CREATE TABLE `atip_ml_feature` (
  `feature_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `description` TEXT,
  `category` TEXT,
  `data_type` TEXT,
  `calculation_method` TEXT,
  `version` TEXT NOT NULL,
  `dependencies_json` TEXT,
  `availability` TEXT,
  `lookback` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`feature_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_feature_set`;
CREATE TABLE `atip_ml_feature_set` (
  `name` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `features_json` TEXT NOT NULL,
  `feature_versions_json` TEXT NOT NULL,
  `description` TEXT,
  `content_hash` TEXT NOT NULL,
  `created_at` DATETIME(6),
  PRIMARY KEY (`name`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_health_check`;
CREATE TABLE `atip_ml_health_check` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `checked_at` DATETIME(6),
  `status` TEXT,
  `result_json` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_label`;
CREATE TABLE `atip_ml_label` (
  `label_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `version` TEXT NOT NULL,
  `kind` TEXT NOT NULL,
  `task` TEXT NOT NULL,
  `spec_json` TEXT NOT NULL,
  `spec_hash` TEXT NOT NULL,
  `description` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`label_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model`;
CREATE TABLE `atip_ml_model` (
  `model_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `description` TEXT,
  `model_type` TEXT NOT NULL,
  `task` TEXT NOT NULL,
  `label_kind` TEXT NOT NULL,
  `feature_set` TEXT NOT NULL,
  `purpose` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `active_version` TEXT,
  `owner` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`model_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model_event`;
CREATE TABLE `atip_ml_model_event` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `model_id` TEXT NOT NULL,
  `version` TEXT,
  `event_type` TEXT NOT NULL,
  `from_state` TEXT,
  `to_state` TEXT,
  `message` TEXT,
  `details_json` TEXT,
  `actor` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model_explanation`;
CREATE TABLE `atip_ml_model_explanation` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `model_id` TEXT NOT NULL,
  `version` TEXT NOT NULL,
  `kind` TEXT NOT NULL,
  `explanation_version` TEXT,
  `payload_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model_metrics`;
CREATE TABLE `atip_ml_model_metrics` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `model_id` TEXT NOT NULL,
  `version` TEXT NOT NULL,
  `kind` TEXT NOT NULL,
  `period_start` DATE,
  `period_end` DATE,
  `metrics_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model_monitoring`;
CREATE TABLE `atip_ml_model_monitoring` (
  `model_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `version_status` TEXT,
  `prediction_dist_json` TEXT,
  `feature_drift_json` TEXT,
  `data_quality_json` TEXT,
  `n_shifted` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`model_id`, `version`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_model_version`;
CREATE TABLE `atip_ml_model_version` (
  `model_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `status` TEXT NOT NULL,
  `feature_set` TEXT,
  `feature_set_hash` TEXT,
  `dataset_id` TEXT,
  `dataset_spec_hash` TEXT,
  `dataset_snapshot_hash` TEXT,
  `training_config_json` TEXT,
  `training_config_hash` TEXT,
  `train_start` DATE,
  `train_end` DATE,
  `artifact_path` TEXT,
  `artifact_hash` TEXT,
  `metrics_json` TEXT,
  `error` TEXT,
  `created_at` DATETIME(6),
  `trained_at` DATETIME(6),
  `activated_at` DATETIME(6),
  PRIMARY KEY (`model_id`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_prediction`;
CREATE TABLE `atip_ml_prediction` (
  `prediction_id` VARCHAR(191) NOT NULL,
  `model_id` VARCHAR(191) NOT NULL,
  `model_version` VARCHAR(191) NOT NULL,
  `version_status` TEXT,
  `symbol` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `prediction` TEXT,
  `prediction_value` DOUBLE,
  `probabilities_json` TEXT,
  `confidence` DOUBLE,
  `prob_up` DOUBLE,
  `ml_score` DOUBLE,
  `interval_low` DOUBLE,
  `interval_high` DOUBLE,
  `feature_set` TEXT,
  `feature_set_hash` TEXT,
  `artifact_hash` TEXT,
  `explanation_json` TEXT,
  `features_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`prediction_id`),
  KEY `idx_ml_prediction_date` (`as_of`, `model_id`),
  UNIQUE KEY `uq_ml_prediction_model_id_model_version_symbol_as_of` (`model_id`, `model_version`, `symbol`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_rl_run`;
CREATE TABLE `atip_ml_rl_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `verdict` TEXT,
  `result_json` TEXT,
  `q_table_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_training_run`;
CREATE TABLE `atip_ml_training_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `model_id` TEXT NOT NULL,
  `version` TEXT,
  `dataset_id` TEXT,
  `status` TEXT NOT NULL,
  `config_json` TEXT,
  `metrics_json` TEXT,
  `rows` BIGINT,
  `error` TEXT,
  `traceback` TEXT,
  `actor` TEXT,
  `started_at` DATETIME(6),
  `finished_at` DATETIME(6),
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ml_validation_report`;
CREATE TABLE `atip_ml_validation_report` (
  `report_id` VARCHAR(191) NOT NULL,
  `model_type` TEXT,
  `dataset_id` TEXT,
  `label_json` TEXT,
  `params_json` TEXT,
  `report_json` TEXT,
  `verdict` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`report_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_news_articles`;
CREATE TABLE `atip_news_articles` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `fetched_at` DATETIME(6) NOT NULL,
  `headline` TEXT NOT NULL,
  `source` TEXT,
  `url` TEXT,
  `category` TEXT,
  `symbols_mentioned` TEXT,
  `sentiment` DOUBLE,
  `importance` TEXT,
  `confidence` DOUBLE,
  `news_score` DOUBLE,
  `ai_summary` TEXT,
  `processed` BIGINT,
  `created_at` DATETIME(6),
  `classifier` TEXT,
  `novelty` DOUBLE,
  `dup_of` BIGINT,
  `source_weight` DOUBLE,
  `half_life_h` DOUBLE,
  `published_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_news_date` (`fetched_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_news_source_quality`;
CREATE TABLE `atip_news_source_quality` (
  `source` VARCHAR(191) NOT NULL,
  `articles` BIGINT,
  `duplicate_share` DOUBLE,
  `symbol_share` DOUBLE,
  `reaction_hit_rate` DOUBLE,
  `reaction_n` BIGINT,
  `configured_weight` DOUBLE,
  `weight` DOUBLE,
  `detail_json` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_news_source_status`;
CREATE TABLE `atip_news_source_status` (
  `source` VARCHAR(191) NOT NULL,
  `url` TEXT,
  `last_attempt` DATETIME(6),
  `last_ok` DATETIME(6),
  `last_items` BIGINT,
  `consecutive_failures` BIGINT,
  `last_error` TEXT,
  PRIMARY KEY (`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_news_summary`;
CREATE TABLE `atip_news_summary` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `created_at` DATETIME(6),
  `window_hours` BIGINT,
  `article_count` BIGINT,
  `classifier` TEXT,
  `model` TEXT,
  `summary_json` TEXT,
  `fallback_reason` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_news_symbol_score`;
CREATE TABLE `atip_news_symbol_score` (
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `score` DOUBLE,
  `n_articles` BIGINT,
  `effective_weight` DOUBLE,
  `mean_sentiment` DOUBLE,
  `components_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_event_delivery`;
CREATE TABLE `atip_oms_event_delivery` (
  `event_id` VARCHAR(191) NOT NULL,
  `handler` VARCHAR(191) NOT NULL,
  `status` TEXT NOT NULL,
  `attempts` BIGINT NOT NULL,
  `error` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`event_id`, `handler`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_event_outbox`;
CREATE TABLE `atip_oms_event_outbox` (
  `seq` BIGINT NOT NULL AUTO_INCREMENT,
  `event_id` VARCHAR(191) NOT NULL,
  `topic` VARCHAR(191) NOT NULL,
  `key` TEXT,
  `payload_json` TEXT,
  `created_at` DATETIME(6),
  `dispatched_at` DATETIME(6),
  `attempts` BIGINT NOT NULL,
  `last_error` TEXT,
  PRIMARY KEY (`seq`),
  KEY `idx_outbox_topic` (`topic`, `created_at`),
  KEY `idx_outbox_pending` (`dispatched_at`, `seq`),
  UNIQUE KEY `uq_oms_event_outbox_event_id` (`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_execution`;
CREATE TABLE `atip_oms_execution` (
  `execution_id` VARCHAR(191) NOT NULL,
  `order_id` VARCHAR(191) NOT NULL,
  `adapter` TEXT,
  `action` TEXT,
  `request_json` TEXT,
  `response_json` TEXT,
  `status` TEXT,
  `broker_order_id` TEXT,
  `error` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`execution_id`),
  KEY `idx_oms_execution_order` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_fill`;
CREATE TABLE `atip_oms_fill` (
  `fill_id` VARCHAR(191) NOT NULL,
  `order_id` TEXT NOT NULL,
  `execution_id` TEXT,
  `strategy_id` VARCHAR(191),
  `strategy_version` TEXT,
  `symbol` VARCHAR(191) NOT NULL,
  `side` TEXT NOT NULL,
  `quantity` BIGINT NOT NULL,
  `price` DOUBLE NOT NULL,
  `fees` DOUBLE NOT NULL,
  `price_source` TEXT,
  `mode` TEXT,
  `filled_at` DATETIME(6),
  PRIMARY KEY (`fill_id`),
  KEY `idx_oms_fill_strategy` (`strategy_id`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_order`;
CREATE TABLE `atip_oms_order` (
  `order_id` VARCHAR(191) NOT NULL,
  `intent_id` VARCHAR(191) NOT NULL,
  `risk_decision_id` VARCHAR(191) NOT NULL,
  `decision_id` TEXT,
  `strategy_id` TEXT,
  `strategy_version` TEXT,
  `symbol` TEXT NOT NULL,
  `side` TEXT NOT NULL,
  `quantity` BIGINT NOT NULL,
  `order_type` TEXT NOT NULL,
  `limit_price` DOUBLE,
  `product_type` TEXT,
  `mode` TEXT NOT NULL,
  `adapter` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `broker_order_id` TEXT,
  `filled_quantity` BIGINT NOT NULL,
  `avg_fill_price` DOUBLE,
  `fees` DOUBLE NOT NULL,
  `reference_price` DOUBLE,
  `reason` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `tenant_id` TEXT,
  `trigger_price` DOUBLE,
  `parent_order_id` TEXT,
  `modified_count` BIGINT,
  `instrument` TEXT,
  `algo_parent_id` TEXT,
  `algo_slice` BIGINT,
  PRIMARY KEY (`order_id`),
  KEY `idx_oms_order_status` (`status`, `created_at`),
  UNIQUE KEY `uq_oms_order_risk_decision_id` (`risk_decision_id`),
  UNIQUE KEY `uq_oms_order_intent_id` (`intent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_oms_order_event`;
CREATE TABLE `atip_oms_order_event` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `order_id` VARCHAR(191) NOT NULL,
  `from_status` TEXT,
  `to_status` TEXT NOT NULL,
  `message` TEXT,
  `details_json` TEXT,
  `actor` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_oms_order_event` (`order_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_alert`;
CREATE TABLE `atip_ops_alert` (
  `rule` VARCHAR(191) NOT NULL,
  `status` TEXT NOT NULL,
  `severity` TEXT,
  `message` TEXT,
  `first_at` DATETIME(6),
  `last_at` DATETIME(6),
  `resolved_at` DATETIME(6),
  `notified_at` DATETIME(6),
  `count` BIGINT NOT NULL,
  PRIMARY KEY (`rule`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_backup`;
CREATE TABLE `atip_ops_backup` (
  `backup_id` VARCHAR(191) NOT NULL,
  `kind` TEXT NOT NULL,
  `path` TEXT,
  `started_at` DATETIME(6),
  `finished_at` DATETIME(6),
  `size_bytes` BIGINT,
  `sha256` TEXT,
  `integrity` TEXT,
  `tables_json` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `error` TEXT,
  `pruned_at` DATETIME(6),
  `offsite_path` TEXT,
  `offsite_status` TEXT,
  `encrypted_sha256` TEXT,
  PRIMARY KEY (`backup_id`),
  KEY `idx_ops_backup_status` (`status`, `finished_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_config_version`;
CREATE TABLE `atip_ops_config_version` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `fingerprint` TEXT NOT NULL,
  `environment` TEXT,
  `config_json` TEXT,
  `changes_json` TEXT,
  `recorded_at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_heartbeat`;
CREATE TABLE `atip_ops_heartbeat` (
  `component` VARCHAR(191) NOT NULL,
  `beat_at` DATETIME(6),
  `pid` BIGINT,
  `detail` TEXT,
  PRIMARY KEY (`component`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_idempotency`;
CREATE TABLE `atip_ops_idempotency` (
  `idem_key` VARCHAR(191) NOT NULL,
  `caller` VARCHAR(191) NOT NULL,
  `request_hash` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `response_status` BIGINT,
  `response_body` LONGBLOB,
  `content_type` TEXT,
  `created_at` DATETIME(6),
  `expires_at` DATETIME(6),
  PRIMARY KEY (`idem_key`, `caller`),
  KEY `idx_ops_idem_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_job_lock`;
CREATE TABLE `atip_ops_job_lock` (
  `job` VARCHAR(191) NOT NULL,
  `owner` TEXT NOT NULL,
  `pid` BIGINT,
  `acquired_at` DATETIME(6),
  `heartbeat_at` DATETIME(6),
  `expires_at` DATETIME(6),
  PRIMARY KEY (`job`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_restore_drill`;
CREATE TABLE `atip_ops_restore_drill` (
  `drill_id` VARCHAR(191) NOT NULL,
  `backup_id` TEXT,
  `source` TEXT,
  `started_at` DATETIME(6),
  `finished_at` DATETIME(6),
  `seconds` DOUBLE,
  `status` TEXT,
  `details_json` TEXT,
  PRIMARY KEY (`drill_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_rollback_drill`;
CREATE TABLE `atip_ops_rollback_drill` (
  `drill_id` VARCHAR(191) NOT NULL,
  `from_ref` TEXT,
  `to_ref` TEXT,
  `started_at` DATETIME(6),
  `finished_at` DATETIME(6),
  `rto_seconds` DOUBLE,
  `status` TEXT,
  `details_json` TEXT,
  PRIMARY KEY (`drill_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_secret_access`;
CREATE TABLE `atip_ops_secret_access` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(191) NOT NULL,
  `source` TEXT,
  `found` BIGINT,
  `caller` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_ops_secret_access` (`name`, `at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_secret_meta`;
CREATE TABLE `atip_ops_secret_meta` (
  `name` VARCHAR(191) NOT NULL,
  `rotated_at` DATETIME(6),
  `rotated_by` TEXT,
  `note` TEXT,
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_webhook_delivery`;
CREATE TABLE `atip_ops_webhook_delivery` (
  `delivery_id` VARCHAR(191) NOT NULL,
  `endpoint_id` VARCHAR(191) NOT NULL,
  `event_type` TEXT,
  `event_id` VARCHAR(191),
  `payload_json` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `attempts` BIGINT NOT NULL,
  `next_attempt_at` DATETIME(6),
  `last_status_code` BIGINT,
  `last_error` TEXT,
  `created_at` DATETIME(6),
  `delivered_at` DATETIME(6),
  PRIMARY KEY (`delivery_id`),
  KEY `idx_ops_wh_delivery` (`status`, `next_attempt_at`),
  UNIQUE KEY `uq_ops_webhook_delivery_endpoint_id_event_id` (`endpoint_id`, `event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_webhook_endpoint`;
CREATE TABLE `atip_ops_webhook_endpoint` (
  `endpoint_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT,
  `url` TEXT NOT NULL,
  `events_json` TEXT,
  `secret_name` TEXT,
  `status` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  PRIMARY KEY (`endpoint_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_ops_webhook_event`;
CREATE TABLE `atip_ops_webhook_event` (
  `source` VARCHAR(191) NOT NULL,
  `event_id` VARCHAR(191) NOT NULL,
  `received_at` DATETIME(6),
  `signature_ok` BIGINT,
  `status` TEXT,
  `payload_sha256` TEXT,
  `event_type` TEXT,
  `error` TEXT,
  PRIMARY KEY (`source`, `event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_option_chain_snapshot`;
CREATE TABLE `atip_option_chain_snapshot` (
  `ts` DATETIME(6) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `expiry` DATE NOT NULL,
  `strike` DOUBLE NOT NULL,
  `option_type` VARCHAR(191) NOT NULL,
  `ltp` DOUBLE,
  `change` DOUBLE,
  `iv` DOUBLE,
  `oi` DOUBLE,
  `oi_chg` DOUBLE,
  `volume` DOUBLE,
  `bid` DOUBLE,
  `ask` DOUBLE,
  `bid_qty` DOUBLE,
  `ask_qty` DOUBLE,
  `underlying` DOUBLE,
  PRIMARY KEY (`ts`, `symbol`, `expiry`, `strike`, `option_type`),
  KEY `idx_ocs_sym` (`symbol`, `ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_options_analytics`;
CREATE TABLE `atip_options_analytics` (
  `instrument_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `iv` DOUBLE,
  `delta` DOUBLE,
  `gamma` DOUBLE,
  `theta` DOUBLE,
  `vega` DOUBLE,
  `rho` DOUBLE,
  `iv_rank` DOUBLE,
  `iv_rv_spread` DOUBLE,
  `model` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`instrument_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_order_book_snapshot`;
CREATE TABLE `atip_order_book_snapshot` (
  `symbol` VARCHAR(191) NOT NULL,
  `ts` DATETIME(6) NOT NULL,
  `ltp` DOUBLE,
  `best_bid` DOUBLE,
  `best_ask` DOUBLE,
  `mid` DOUBLE,
  `spread_bps` DOUBLE,
  `bid_qty_5` DOUBLE,
  `ask_qty_5` DOUBLE,
  `imbalance` DOUBLE,
  `bids_json` TEXT,
  `asks_json` TEXT,
  `source` TEXT,
  PRIMARY KEY (`symbol`, `ts`),
  KEY `idx_obs_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_order_log`;
CREATE TABLE `atip_order_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `timestamp` TEXT,
  `symbol` TEXT,
  `transaction_type` TEXT,
  `quantity` BIGINT,
  `order_type` TEXT,
  `product_type` TEXT,
  `price` DOUBLE,
  `estimated_value` DOUBLE,
  `available_funds` DOUBLE,
  `mode` TEXT,
  `status` TEXT,
  `dhan_order_id` TEXT,
  `error` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_order_rules`;
CREATE TABLE `atip_order_rules` (
  `id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `side` TEXT NOT NULL,
  `trigger_type` TEXT NOT NULL,
  `trigger_value` DOUBLE,
  `trigger_percent` DOUBLE,
  `reference_price` DOUBLE NOT NULL,
  `resolved_trigger_price` DOUBLE NOT NULL,
  `quantity_type` TEXT NOT NULL,
  `quantity_value` DOUBLE NOT NULL,
  `stoploss_type` TEXT,
  `stoploss_value` DOUBLE,
  `resolved_stoploss_price` DOUBLE,
  `product_type` TEXT NOT NULL,
  `order_type` TEXT NOT NULL,
  `limit_price` DOUBLE,
  `require_confirmation` BIGINT NOT NULL,
  `status` VARCHAR(191) NOT NULL,
  `notes` TEXT,
  `created_at` TEXT NOT NULL,
  `updated_at` TEXT NOT NULL,
  `triggered_at` TEXT,
  `trigger_hit_price` DOUBLE,
  `confirmation_expires_at` TEXT,
  `dhan_order_id` TEXT,
  `execution_price` DOUBLE,
  `execution_quantity` DOUBLE,
  `execution_error` TEXT,
  `trigger_direction` TEXT,
  `role` TEXT,
  `parent_rule_id` TEXT,
  `oco_group_id` TEXT,
  `bracket_target_pct` DOUBLE,
  `bracket_target2_pct` DOUBLE,
  `bracket_stop_pct` DOUBLE,
  `bracket_auto_exit` BIGINT,
  `bracket_target_split` DOUBLE,
  `trail_enabled` BIGINT,
  `trail_type` TEXT,
  `trail_value` DOUBLE,
  `trail_jump` DOUBLE,
  `trail_high_water` DOUBLE,
  `trail_moves` BIGINT,
  `broker_leg_type` TEXT,
  `broker_leg_id` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_order_rules_status` (`status`),
  KEY `idx_order_rules_symbol` (`symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_account`;
CREATE TABLE `atip_paper_account` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `balance` DOUBLE NOT NULL,
  `opened_at` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_futures_position`;
CREATE TABLE `atip_paper_futures_position` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `strategy_id` VARCHAR(191) NOT NULL,
  `underlying` VARCHAR(191) NOT NULL,
  `expiry` DATE NOT NULL,
  `lots` BIGINT NOT NULL,
  `lot_size` BIGINT NOT NULL,
  `avg_price` DOUBLE NOT NULL,
  `realized_pnl` DOUBLE NOT NULL,
  `margin_blocked` DOUBLE NOT NULL,
  `opened_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_paper_futures_position_strategy_id_underlying_expiry` (`strategy_id`, `underlying`, `expiry`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_futures_trade`;
CREATE TABLE `atip_paper_futures_trade` (
  `trade_id` VARCHAR(191) NOT NULL,
  `order_id` TEXT,
  `strategy_id` TEXT,
  `underlying` TEXT,
  `expiry` DATE,
  `side` TEXT,
  `lots` BIGINT,
  `lot_size` BIGINT,
  `price` DOUBLE,
  `fees` DOUBLE,
  `reason` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`trade_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_options_position`;
CREATE TABLE `atip_paper_options_position` (
  `underlying` VARCHAR(191) NOT NULL,
  `expiry` DATE NOT NULL,
  `strike` DOUBLE NOT NULL,
  `option_type` VARCHAR(191) NOT NULL,
  `qty` DOUBLE NOT NULL,
  `lot_size` BIGINT,
  `avg_price` DOUBLE,
  `realized` DOUBLE NOT NULL,
  `opened_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`underlying`, `expiry`, `strike`, `option_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_options_trade`;
CREATE TABLE `atip_paper_options_trade` (
  `trade_id` VARCHAR(191) NOT NULL,
  `underlying` TEXT,
  `expiry` DATE,
  `strike` DOUBLE,
  `option_type` TEXT,
  `side` TEXT,
  `lots` BIGINT,
  `lot_size` BIGINT,
  `qty` DOUBLE,
  `price` DOUBLE,
  `premium` DOUBLE,
  `fees` DOUBLE,
  `price_source` TEXT,
  `reason` TEXT,
  `realized` DOUBLE,
  `at` DATETIME(6),
  PRIMARY KEY (`trade_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_order`;
CREATE TABLE `atip_paper_order` (
  `order_id` VARCHAR(191) NOT NULL,
  `created_at` TEXT,
  `symbol` TEXT,
  `security_id` TEXT,
  `exchange` TEXT,
  `transaction_type` TEXT,
  `quantity` BIGINT,
  `filled_qty` BIGINT,
  `order_type` TEXT,
  `product_type` TEXT,
  `limit_price` DOUBLE,
  `fill_price` DOUBLE,
  `status` TEXT,
  `reason` TEXT,
  `brokerage` DOUBLE,
  `tag` TEXT,
  `trigger_price` DOUBLE,
  `triggered_at` TEXT,
  `updated_at` TEXT,
  `modifications` BIGINT,
  PRIMARY KEY (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_paper_position`;
CREATE TABLE `atip_paper_position` (
  `symbol` VARCHAR(191) NOT NULL,
  `quantity` BIGINT,
  `avg_price` DOUBLE,
  `realized_pnl` DOUBLE,
  `updated_at` TEXT,
  PRIMARY KEY (`symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_perf_ledger`;
CREATE TABLE `atip_perf_ledger` (
  `txn_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `portfolio` VARCHAR(191) NOT NULL,
  `source` VARCHAR(191) NOT NULL,
  `source_ref` VARCHAR(191) NOT NULL,
  `trade_date` DATE NOT NULL,
  `ts` TEXT,
  `kind` TEXT NOT NULL,
  `symbol` TEXT,
  `quantity` DOUBLE,
  `price` DOUBLE,
  `gross_value` DOUBLE,
  `fees` DOUBLE,
  `reference_price` DOUBLE,
  `price_quality` TEXT,
  `strategy_id` TEXT,
  `tag` TEXT,
  `note` TEXT,
  `created_at` DATETIME(6),
  `import_run` TEXT,
  PRIMARY KEY (`txn_id`),
  KEY `idx_perf_ledger_pf` (`tenant_id`, `owner_id`, `portfolio`, `trade_date`),
  UNIQUE KEY `uq_perf_ledger_tenant_id_owner_id_source_source_ref` (`tenant_id`, `owner_id`, `source`, `source_ref`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_perf_ledger_void`;
CREATE TABLE `atip_perf_ledger_void` (
  `txn_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `owner_id` TEXT NOT NULL,
  `voided_at` DATETIME(6),
  `voided_by` TEXT,
  `reason` TEXT,
  PRIMARY KEY (`txn_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_perf_report_run`;
CREATE TABLE `atip_perf_report_run` (
  `report_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `portfolio` TEXT,
  `period_start` DATE,
  `period_end` DATE,
  `benchmark` TEXT,
  `methodology_version` TEXT,
  `calculation_version` TEXT,
  `inputs_hash` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  PRIMARY KEY (`report_id`),
  KEY `idx_perf_report_owner` (`tenant_id`, `owner_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_pipeline_log`;
CREATE TABLE `atip_pipeline_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `run_date` DATE,
  `job_name` VARCHAR(191) NOT NULL,
  `start_time` DATETIME(6),
  `end_time` DATETIME(6),
  `status` VARCHAR(191),
  `rows_processed` BIGINT,
  `error_msg` TEXT,
  `created_at` DATETIME(6),
  `kind` TEXT,
  `duration_s` DOUBLE,
  PRIMARY KEY (`id`),
  KEY `idx_pipeline_log_status` (`status`, `start_time`),
  KEY `idx_pipeline_log_job` (`job_name`, `start_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_pnl_daily`;
CREATE TABLE `atip_pnl_daily` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `env` VARCHAR(191) NOT NULL,
  `n_positions` BIGINT,
  `positions_value` DOUBLE,
  `cost` DOUBLE,
  `unrealised` DOUBLE,
  `realised_cum` DOUBLE,
  `cash` DOUBLE,
  `equity` DOUBLE,
  `day_pnl` DOUBLE,
  `peak_equity` DOUBLE,
  `drawdown_pct` DOUBLE,
  `recorded_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_pnl_daily_date_env` (`date`, `env`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_portfolio_holdings`;
CREATE TABLE `atip_portfolio_holdings` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `qty` BIGINT,
  `avg_price` DOUBLE,
  `cmp` DOUBLE,
  `current_val` DOUBLE,
  `pnl` DOUBLE,
  `pnl_pct` DOUBLE,
  `atip_score` DOUBLE,
  `vpi` DOUBLE,
  `cri` DOUBLE,
  `zpi` DOUBLE,
  `signal` TEXT,
  `weight_pct` DOUBLE,
  `sector` TEXT,
  `beta_1y` DOUBLE,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_portfolio_holdings_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_portfolio_optimization`;
CREATE TABLE `atip_portfolio_optimization` (
  `opt_id` VARCHAR(191) NOT NULL,
  `as_of` DATE,
  `objective` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`opt_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_portfolio_rebalance_plan`;
CREATE TABLE `atip_portfolio_rebalance_plan` (
  `plan_id` VARCHAR(191) NOT NULL,
  `book` TEXT,
  `as_of` DATE,
  `plan_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_portfolio_risk_snapshot`;
CREATE TABLE `atip_portfolio_risk_snapshot` (
  `as_of` DATE NOT NULL,
  `book` VARCHAR(191) NOT NULL,
  `headline_json` TEXT,
  `analysis_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`as_of`, `book`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_portfolio_sync`;
CREATE TABLE `atip_portfolio_sync` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `date` DATE NOT NULL,
  `source` TEXT NOT NULL,
  `status` VARCHAR(191) NOT NULL,
  `n_holdings` BIGINT,
  `error` TEXT,
  `synced_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_portfolio_sync_date` (`date`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_predictions`;
CREATE TABLE `atip_predictions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `pred_date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `signal` TEXT,
  `atip_score` DOUBLE,
  `vpi` DOUBLE,
  `zpi` DOUBLE,
  `mri` DOUBLE,
  `cri` DOUBLE,
  `acs` DOUBLE,
  `entry_price` DOUBLE,
  `stop_loss` DOUBLE,
  `target_1` DOUBLE,
  `target_2` DOUBLE,
  `risk_reward` DOUBLE,
  `position_size_pct` DOUBLE,
  `confidence` DOUBLE,
  `reasoning` TEXT,
  `regime` TEXT,
  `is_tod` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_predictions_pred_date_symbol` (`pred_date`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_prices_daily`;
CREATE TABLE `atip_prices_daily` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `open` DOUBLE,
  `high` DOUBLE,
  `low` DOUBLE,
  `close` DOUBLE,
  `adj_close` DOUBLE,
  `volume` BIGINT,
  `delivery_qty` BIGINT,
  `delivery_pct` DOUBLE,
  `series` TEXT,
  `source` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_prices_date` (`date`),
  KEY `idx_prices_symbol_date` (`symbol`, `date`),
  UNIQUE KEY `uq_prices_daily_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_composite`;
CREATE TABLE `atip_quant_composite` (
  `name` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `components_json` TEXT NOT NULL,
  `min_coverage` DOUBLE,
  `normalization_json` TEXT,
  `description` TEXT,
  `content_hash` TEXT NOT NULL,
  `status` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`name`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_experiment`;
CREATE TABLE `atip_quant_experiment` (
  `experiment_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `version` TEXT,
  `hypothesis` TEXT,
  `config_json` TEXT,
  `config_hash` TEXT,
  `status` TEXT NOT NULL,
  `backtest_run_ids_json` TEXT,
  `error` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`experiment_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_exposure`;
CREATE TABLE `atip_quant_exposure` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `portfolio_id` TEXT,
  `as_of` DATE,
  `exposure_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_factor`;
CREATE TABLE `atip_quant_factor` (
  `factor_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `name` TEXT,
  `category` TEXT,
  `description` TEXT,
  `inputs_json` TEXT,
  `formula` TEXT,
  `lookback` BIGINT,
  `frequency` TEXT,
  `normalization_json` TEXT,
  `direction` BIGINT,
  `data_dependency` TEXT,
  `status` TEXT,
  `content_hash` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`factor_id`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_factor_approval`;
CREATE TABLE `atip_quant_factor_approval` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `factor_key` VARCHAR(191) NOT NULL,
  `decision` TEXT NOT NULL,
  `verdict` TEXT,
  `evidence_json` TEXT,
  `reason` TEXT,
  `decided_at` DATETIME(6),
  `decided_by` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_qfa_key` (`factor_key`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_factor_research`;
CREATE TABLE `atip_quant_factor_research` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `factor_key` TEXT NOT NULL,
  `kind` TEXT NOT NULL,
  `start_date` DATE,
  `end_date` DATE,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_factor_score`;
CREATE TABLE `atip_quant_factor_score` (
  `as_of` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `factor_key` VARCHAR(191) NOT NULL,
  `kind` TEXT NOT NULL,
  `raw` DOUBLE,
  `norm` DOUBLE,
  `pct` DOUBLE,
  `score` DOUBLE,
  `rank` BIGINT,
  `sector` TEXT,
  `sector_rank` BIGINT,
  `universe_size` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`as_of`, `symbol`, `factor_key`),
  KEY `idx_qfs_key_date` (`factor_key`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_factor_set`;
CREATE TABLE `atip_quant_factor_set` (
  `name` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `factors_json` TEXT NOT NULL,
  `content_hash` TEXT NOT NULL,
  `description` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`name`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_pair`;
CREATE TABLE `atip_quant_pair` (
  `pair_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `asset_a` TEXT NOT NULL,
  `asset_b` TEXT NOT NULL,
  `hedge_ratio` TEXT,
  `spread_kind` TEXT,
  `lookback` BIGINT,
  `entry_z` DOUBLE,
  `exit_z` DOUBLE,
  `stop_z` DOUBLE,
  `capital_allocation_pct` DOUBLE,
  `status` TEXT,
  `notes` TEXT,
  `created_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`pair_id`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_portfolio`;
CREATE TABLE `atip_quant_portfolio` (
  `portfolio_id` VARCHAR(191) NOT NULL,
  `name` TEXT,
  `as_of` DATE,
  `spec_json` TEXT,
  `method` TEXT,
  `long_short` TEXT,
  `cash` DOUBLE,
  `created_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`portfolio_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_portfolio_position`;
CREATE TABLE `atip_quant_portfolio_position` (
  `portfolio_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `weight` DOUBLE,
  `side` TEXT,
  `sector` TEXT,
  PRIMARY KEY (`portfolio_id`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_quant_spread`;
CREATE TABLE `atip_quant_spread` (
  `pair_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `hedge_ratio` DOUBLE,
  `spread` DOUBLE,
  `zscore` DOUBLE,
  `correlation` DOUBLE,
  `adf_t` DOUBLE,
  `cointegrated_5pct` BIGINT,
  `half_life` DOUBLE,
  `sessions` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`pair_id`, `version`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_reconciliation_run`;
CREATE TABLE `atip_reconciliation_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `trade_date` DATE,
  `run_at` DATETIME(6),
  `status` TEXT,
  `breaks` BIGINT,
  `explained` BIGINT,
  `details_json` TEXT,
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_regulatory_item`;
CREATE TABLE `atip_regulatory_item` (
  `item_id` VARCHAR(191) NOT NULL,
  `area` TEXT NOT NULL,
  `title` TEXT NOT NULL,
  `confirm` TEXT NOT NULL,
  `gate` TEXT NOT NULL,
  `status` TEXT NOT NULL,
  `reviewer` TEXT,
  `reference` TEXT,
  `note` TEXT,
  `updated_at` DATETIME(6),
  `signed_at` DATETIME(6),
  PRIMARY KEY (`item_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_research_link`;
CREATE TABLE `atip_research_link` (
  `study_id` VARCHAR(191) NOT NULL,
  `kind` VARCHAR(191) NOT NULL,
  `ref` VARCHAR(191) NOT NULL,
  `note` TEXT,
  `added_at` DATETIME(6),
  `added_by` TEXT,
  PRIMARY KEY (`study_id`, `kind`, `ref`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_research_study`;
CREATE TABLE `atip_research_study` (
  `study_id` VARCHAR(191) NOT NULL,
  `title` TEXT NOT NULL,
  `hypothesis` TEXT NOT NULL,
  `method` TEXT,
  `status` TEXT NOT NULL,
  `outcome` TEXT,
  `conclusion` TEXT,
  `tags_json` TEXT,
  `supersedes` TEXT,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `created_by` TEXT,
  `concluded_by` TEXT,
  PRIMARY KEY (`study_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_risk_decision`;
CREATE TABLE `atip_risk_decision` (
  `risk_decision_id` VARCHAR(191) NOT NULL,
  `intent_id` VARCHAR(191) NOT NULL,
  `decision_id` TEXT,
  `strategy_id` TEXT,
  `strategy_version` TEXT,
  `symbol` TEXT NOT NULL,
  `side` TEXT,
  `action` TEXT,
  `book` TEXT,
  `mode` TEXT,
  `requested_quantity` BIGINT,
  `approved_quantity` BIGINT,
  `reference_price` DOUBLE,
  `est_value` DOUBLE,
  `equity` DOUBLE,
  `risk_status` VARCHAR(191) NOT NULL,
  `rejection_reason` TEXT,
  `risk_checks_json` TEXT,
  `limits_json` TEXT,
  `engine_version` TEXT,
  `reviewed_by` TEXT,
  `reviewed_at` DATETIME(6),
  `created_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`risk_decision_id`),
  KEY `idx_risk_decision_created` (`created_at`, `risk_status`),
  KEY `idx_risk_decision_intent` (`intent_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_risk_emergency_exit`;
CREATE TABLE `atip_risk_emergency_exit` (
  `run_id` VARCHAR(191) NOT NULL,
  `book` TEXT,
  `actor` TEXT,
  `reason` TEXT,
  `status` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_risk_limit`;
CREATE TABLE `atip_risk_limit` (
  `key` VARCHAR(191) NOT NULL,
  `value_json` TEXT,
  `note` TEXT,
  `updated_by` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_risk_limit_history`;
CREATE TABLE `atip_risk_limit_history` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `key` TEXT NOT NULL,
  `old_json` TEXT,
  `new_json` TEXT,
  `actor` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_sast_disclosure`;
CREATE TABLE `atip_sast_disclosure` (
  `disclosure_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `acquirer` TEXT,
  `is_promoter` BIGINT,
  `txn_type` TEXT,
  `shares_acq` DOUBLE,
  `shares_sold` DOUBLE,
  `post_pct` DOUBLE,
  `disclosed_at` DATETIME(6),
  `fetched_at` DATETIME(6),
  PRIMARY KEY (`disclosure_id`),
  KEY `idx_sast_symbol` (`symbol`, `disclosed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_schema_migrations`;
CREATE TABLE `atip_schema_migrations` (
  `version` VARCHAR(32) NOT NULL,
  `name` TEXT NOT NULL,
  `checksum` TEXT NOT NULL,
  `applied_at` DATETIME(6),
  `duration_ms` DOUBLE,
  `status` TEXT NOT NULL,
  `rollback_note` TEXT,
  `error` TEXT,
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_score_components`;
CREATE TABLE `atip_score_components` (
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `index_name` VARCHAR(191) NOT NULL,
  `component` VARCHAR(191) NOT NULL,
  `value` DOUBLE,
  `weight` DOUBLE,
  PRIMARY KEY (`symbol`, `date`, `index_name`, `component`),
  KEY `idx_score_components_date` (`date`, `index_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_sector_breadth`;
CREATE TABLE `atip_sector_breadth` (
  `date` DATE NOT NULL,
  `sector` VARCHAR(191) NOT NULL,
  `stocks` BIGINT,
  `pct_advancing` DOUBLE,
  `avg_return_pct` DOUBLE,
  `pct_above_50dma` DOUBLE,
  `pct_above_200dma` DOUBLE,
  `created_at` DATETIME(6),
  PRIMARY KEY (`date`, `sector`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_shareholding_pattern`;
CREATE TABLE `atip_shareholding_pattern` (
  `symbol` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `promoter_pct` DOUBLE,
  `public_pct` DOUBLE,
  `mf_pct` DOUBLE,
  `fpi_pct` DOUBLE,
  `insurance_pct` DOUBLE,
  `dii_pct` DOUBLE,
  `retail_pct` DOUBLE,
  `pledged_pct` DOUBLE,
  `submitted_at` DATETIME(6),
  `xbrl_url` TEXT,
  `source` TEXT,
  `fetched_at` DATETIME(6),
  PRIMARY KEY (`symbol`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_signal_log`;
CREATE TABLE `atip_signal_log` (
  `id` VARCHAR(191) NOT NULL,
  `run_id` VARCHAR(191) NOT NULL,
  `logged_at` TEXT NOT NULL,
  `signal_date` DATE NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `signal` TEXT NOT NULL,
  `entry_price` DOUBLE,
  `atip_score` DOUBLE,
  `vpi` DOUBLE,
  `spi` DOUBLE,
  `rri` DOUBLE,
  `mri` DOUBLE,
  `cri` DOUBLE,
  `msi` DOUBLE,
  `zpi` DOUBLE,
  `acs` DOUBLE,
  `mh_score` DOUBLE,
  `regime` TEXT,
  `is_tod` BIGINT,
  `model_version` TEXT,
  `weights_hash` TEXT,
  `notes` TEXT,
  `duplicate_of` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_siglog_run` (`run_id`),
  KEY `idx_siglog_symbol` (`symbol`, `signal_date`),
  KEY `idx_siglog_date` (`signal_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_signal_outcome`;
CREATE TABLE `atip_signal_outcome` (
  `signal_id` VARCHAR(191) NOT NULL,
  `threshold_pct` DOUBLE NOT NULL,
  `hit` BIGINT,
  `hit_date` DATE,
  `sessions_to_hit` BIGINT,
  `hit_price` DOUBLE,
  `max_favourable_pct` DOUBLE,
  `max_adverse_pct` DOUBLE,
  `sessions_tracked` BIGINT,
  `still_open` BIGINT,
  `data_gap_sessions` BIGINT,
  `evaluated_at` TEXT,
  PRIMARY KEY (`signal_id`, `threshold_pct`),
  KEY `idx_sigout_hit` (`threshold_pct`, `hit`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy`;
CREATE TABLE `atip_strategy` (
  `strategy_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `description` TEXT,
  `kind` TEXT NOT NULL,
  `category` TEXT,
  `status` TEXT NOT NULL,
  `current_version` TEXT,
  `source` TEXT,
  `owner` TEXT,
  `priority` BIGINT,
  `weight` DOUBLE,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `activated_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`strategy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_decision`;
CREATE TABLE `atip_strategy_decision` (
  `decision_id` VARCHAR(191) NOT NULL,
  `run_id` TEXT,
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `timestamp` DATETIME(6),
  `symbol` VARCHAR(191) NOT NULL,
  `decision` VARCHAR(191) NOT NULL,
  `action` TEXT NOT NULL,
  `confidence` DOUBLE,
  `score` DOUBLE,
  `regime` TEXT,
  `reasons_json` TEXT,
  `parameters_json` TEXT,
  `risk_requirement` TEXT,
  `target_position_pct` DOUBLE,
  `stop_price` DOUBLE,
  `target_price` DOUBLE,
  `max_hold_sessions` BIGINT,
  `blocked_reason` TEXT,
  `features_json` TEXT,
  `reason_codes_json` TEXT,
  `signal_source` TEXT,
  PRIMARY KEY (`decision_id`),
  KEY `idx_strategy_decision_date` (`as_of`, `decision`),
  UNIQUE KEY `uq_strategy_decision_strategy_id_version_as_of_symbol` (`strategy_id`, `version`, `as_of`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_decision_run`;
CREATE TABLE `atip_strategy_decision_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` TEXT,
  `as_of` DATE,
  `book` TEXT,
  `status` TEXT NOT NULL,
  `error` TEXT,
  `params_json` TEXT,
  `n_universe` BIGINT,
  `n_evaluated` BIGINT,
  `counts_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`run_id`),
  KEY `idx_strategy_decision_run` (`strategy_id`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_engine_event`;
CREATE TABLE `atip_strategy_engine_event` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `strategy_id` VARCHAR(191),
  `version` TEXT,
  `event_type` TEXT NOT NULL,
  `from_state` TEXT,
  `to_state` TEXT,
  `message` TEXT,
  `details_json` TEXT,
  `actor` TEXT,
  `at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_strategy_engine_event` (`strategy_id`, `at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_event`;
CREATE TABLE `atip_strategy_event` (
  `id` VARCHAR(191) NOT NULL,
  `position_id` VARCHAR(191) NOT NULL,
  `event_key` VARCHAR(191) NOT NULL,
  `event_type` TEXT NOT NULL,
  `price` DOUBLE,
  `quantity` BIGINT,
  `detail` TEXT,
  `created_at` TEXT,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_strategy_event_position_id_event_key` (`position_id`, `event_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_feature`;
CREATE TABLE `atip_strategy_feature` (
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `feature` VARCHAR(191) NOT NULL,
  `inputs` TEXT,
  PRIMARY KEY (`strategy_id`, `version`, `feature`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_health`;
CREATE TABLE `atip_strategy_health` (
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `as_of` DATE NOT NULL,
  `status` TEXT NOT NULL,
  `metrics_json` TEXT,
  `issues_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`strategy_id`, `version`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_parameter`;
CREATE TABLE `atip_strategy_parameter` (
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `name` VARCHAR(191) NOT NULL,
  `type` TEXT NOT NULL,
  `default_json` TEXT,
  `min` DOUBLE,
  `max` DOUBLE,
  `allowed_json` TEXT,
  `required` BIGINT NOT NULL,
  `description` TEXT,
  PRIMARY KEY (`strategy_id`, `version`, `name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_position`;
CREATE TABLE `atip_strategy_position` (
  `id` VARCHAR(191) NOT NULL,
  `entry_rule_id` VARCHAR(191),
  `symbol` VARCHAR(191) NOT NULL,
  `side` TEXT NOT NULL,
  `entry_price` DOUBLE NOT NULL,
  `initial_qty` BIGINT NOT NULL,
  `remaining_qty` BIGINT NOT NULL,
  `t1_state` TEXT,
  `t1_qty` BIGINT,
  `t1_price` DOUBLE,
  `t1_at` TEXT,
  `t2_state` TEXT,
  `t2_at` TEXT,
  `t2_verdict` TEXT,
  `high_water` DOUBLE,
  `trail_pct` DOUBLE,
  `trail_stop` DOUBLE,
  `trail_moves` BIGINT,
  `realized_pnl` DOUBLE,
  `unrealized_pnl` DOUBLE,
  `cost_pct` DOUBLE,
  `status` VARCHAR(191),
  `exit_reason` TEXT,
  `exit_price` DOUBLE,
  `closed_at` TEXT,
  `mode` TEXT,
  `created_at` TEXT,
  `updated_at` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_stratpos_entry` (`entry_rule_id`),
  KEY `idx_stratpos_symbol` (`symbol`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_position_intent`;
CREATE TABLE `atip_strategy_position_intent` (
  `intent_id` VARCHAR(191) NOT NULL,
  `decision_id` VARCHAR(191) NOT NULL,
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` TEXT NOT NULL,
  `as_of` DATE NOT NULL,
  `timestamp` DATETIME(6),
  `symbol` TEXT NOT NULL,
  `side` TEXT NOT NULL,
  `action` TEXT,
  `target_position_pct` DOUBLE,
  `quantity` BIGINT,
  `stop_price` DOUBLE,
  `target_price` DOUBLE,
  `max_hold_sessions` BIGINT,
  `confidence` DOUBLE,
  `reason` TEXT,
  `risk_requirement` TEXT,
  `authorization_status` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `entry_reference` DOUBLE,
  `book` TEXT,
  `risk_decision_id` TEXT,
  `authorized_at` DATETIME(6),
  `tenant_id` TEXT,
  PRIMARY KEY (`intent_id`),
  KEY `idx_strategy_intent` (`as_of`, `strategy_id`),
  UNIQUE KEY `uq_strategy_position_intent_decision_id` (`decision_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_regime_mapping`;
CREATE TABLE `atip_strategy_regime_mapping` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `regime` VARCHAR(191) NOT NULL,
  `strategy_id` VARCHAR(191),
  `no_trade` BIGINT NOT NULL,
  `priority` BIGINT NOT NULL,
  `weight` DOUBLE NOT NULL,
  `enabled` BIGINT NOT NULL,
  `notes` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_strategy_regime_mapping_regime_strategy_id` (`regime`, `strategy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_strategy_version`;
CREATE TABLE `atip_strategy_version` (
  `strategy_id` VARCHAR(191) NOT NULL,
  `version` VARCHAR(191) NOT NULL,
  `definition_json` TEXT NOT NULL,
  `definition_hash` TEXT NOT NULL,
  `notes` TEXT,
  `created_at` DATETIME(6),
  `first_activated_at` DATETIME(6),
  PRIMARY KEY (`strategy_id`, `version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_technical_ext`;
CREATE TABLE `atip_technical_ext` (
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `vwap_20d` DOUBLE,
  `vwap_session` DOUBLE,
  `vwap_dev_pct` DOUBLE,
  `vp_poc` DOUBLE,
  `vp_vah` DOUBLE,
  `vp_val` DOUBLE,
  `vp_basis` TEXT,
  `sr_support` DOUBLE,
  `sr_support_touches` BIGINT,
  `sr_support_dist_pct` DOUBLE,
  `sr_resistance` DOUBLE,
  `sr_resistance_touches` BIGINT,
  `sr_resistance_dist_pct` DOUBLE,
  `beta_60` DOUBLE,
  `beta_downside` DOUBLE,
  `beta_long` DOUBLE,
  `beta_long_sessions` BIGINT,
  `bri` DOUBLE,
  `weekly_rsi` DOUBLE,
  `weekly_trend` TEXT,
  `monthly_trend` TEXT,
  `mtf_alignment` BIGINT,
  `supertrend` DOUBLE,
  `supertrend_dir` BIGINT,
  `ichimoku_tenkan` DOUBLE,
  `ichimoku_kijun` DOUBLE,
  `ichimoku_span_a` DOUBLE,
  `ichimoku_span_b` DOUBLE,
  `keltner_upper` DOUBLE,
  `keltner_lower` DOUBLE,
  `donchian_upper` DOUBLE,
  `donchian_lower` DOUBLE,
  `mfi_14` DOUBLE,
  `cmf_20` DOUBLE,
  `roc_10` DOUBLE,
  `aroon_up` DOUBLE,
  `aroon_down` DOUBLE,
  `psar` DOUBLE,
  `created_at` DATETIME(6),
  PRIMARY KEY (`symbol`, `date`),
  KEY `idx_technical_ext_date` (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_technical_indicators`;
CREATE TABLE `atip_technical_indicators` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `symbol` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `rsi_14` DOUBLE,
  `stoch_k` DOUBLE,
  `stoch_d` DOUBLE,
  `williams_r` DOUBLE,
  `cci_20` DOUBLE,
  `macd_line` DOUBLE,
  `macd_signal` DOUBLE,
  `macd_hist` DOUBLE,
  `macd_hist_pct` DOUBLE,
  `adx_14` DOUBLE,
  `ema_9` DOUBLE,
  `ema_21` DOUBLE,
  `ema_50` DOUBLE,
  `sma_200` DOUBLE,
  `atr_14` DOUBLE,
  `atr_pct` DOUBLE,
  `bb_upper` DOUBLE,
  `bb_lower` DOUBLE,
  `bb_mid` DOUBLE,
  `bb_width` DOUBLE,
  `obv` DOUBLE,
  `volume_sma20` DOUBLE,
  `volume_ratio` DOUBLE,
  `rel_volume` DOUBLE,
  `pivot` DOUBLE,
  `r1` DOUBLE,
  `r2` DOUBLE,
  `s1` DOUBLE,
  `s2` DOUBLE,
  `fib_236` DOUBLE,
  `fib_382` DOUBLE,
  `fib_500` DOUBLE,
  `fib_618` DOUBLE,
  `golden_cross` BIGINT,
  `death_cross` BIGINT,
  `above_200dma` BIGINT,
  `gap_pct` DOUBLE,
  `tech_score` DOUBLE,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_tech_symbol_date` (`symbol`, `date`),
  UNIQUE KEY `uq_technical_indicators_symbol_date` (`symbol`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_tenant_paper_account`;
CREATE TABLE `atip_tenant_paper_account` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `starting_cash` DOUBLE NOT NULL,
  `cash` DOUBLE NOT NULL,
  `realized_pnl` DOUBLE,
  `peak_equity` DOUBLE,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_tenant_paper_fill`;
CREATE TABLE `atip_tenant_paper_fill` (
  `fill_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `order_id` TEXT,
  `symbol` TEXT NOT NULL,
  `side` TEXT NOT NULL,
  `quantity` BIGINT NOT NULL,
  `price` DOUBLE NOT NULL,
  `fees` DOUBLE,
  `filled_at` DATETIME(6),
  PRIMARY KEY (`fill_id`),
  KEY `idx_tpf_tenant` (`tenant_id`, `filled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_tenant_paper_position`;
CREATE TABLE `atip_tenant_paper_position` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `quantity` BIGINT NOT NULL,
  `avg_price` DOUBLE NOT NULL,
  `realized_pnl` DOUBLE,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_tenant_pnl_daily`;
CREATE TABLE `atip_tenant_pnl_daily` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `equity` DOUBLE,
  `cash` DOUBLE,
  `positions_value` DOUBLE,
  `day_pnl` DOUBLE,
  `peak_equity` DOUBLE,
  `drawdown_pct` DOUBLE,
  `recorded_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_tick_capture_status`;
CREATE TABLE `atip_tick_capture_status` (
  `day` DATE NOT NULL,
  `ticks` BIGINT,
  `flushed` BIGINT,
  `symbols` BIGINT,
  `dropped` BIGINT,
  `first_tick` DATETIME(6),
  `last_tick` DATETIME(6),
  `minute_bars` BIGINT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_advice_log`;
CREATE TABLE `atip_wealth_advice_log` (
  `advice_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `asked_at` DATETIME(6),
  `asked_by` TEXT,
  `question` TEXT,
  `topic` TEXT,
  `response_json` TEXT,
  `narration_status` TEXT,
  `narration_model` TEXT,
  `feedback` TEXT,
  `feedback_note` TEXT,
  PRIMARY KEY (`advice_id`),
  KEY `idx_wealth_advice_owner` (`tenant_id`, `owner_id`, `asked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_allocation_policy`;
CREATE TABLE `atip_wealth_allocation_policy` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `policy_json` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `owner_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_allocation_run`;
CREATE TABLE `atip_wealth_allocation_run` (
  `run_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `as_of` DATE,
  `methodology_version` TEXT,
  `profile_id` TEXT,
  `band` TEXT,
  `target_json` TEXT,
  `result_json` TEXT,
  `inputs_hash` TEXT,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  PRIMARY KEY (`run_id`),
  KEY `idx_wealth_alloc_owner` (`tenant_id`, `owner_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_classification`;
CREATE TABLE `atip_wealth_classification` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `symbol` VARCHAR(191) NOT NULL,
  `asset_class` TEXT NOT NULL,
  `instrument` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `owner_id`, `symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_cycle_run`;
CREATE TABLE `atip_wealth_cycle_run` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `run_date` DATE,
  `status` TEXT,
  `result_json` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`id`),
  KEY `idx_wealth_cycle_owner` (`tenant_id`, `owner_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_feedback`;
CREATE TABLE `atip_wealth_feedback` (
  `feedback_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `owner_id` TEXT NOT NULL,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  `page` TEXT,
  `category` TEXT,
  `severity` VARCHAR(191),
  `message` TEXT,
  `context_json` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `triage_note` TEXT,
  `triaged_at` DATETIME(6),
  `triaged_by` TEXT,
  PRIMARY KEY (`feedback_id`),
  KEY `idx_wealth_feedback_status` (`status`, `severity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_goal`;
CREATE TABLE `atip_wealth_goal` (
  `goal_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `name` TEXT NOT NULL,
  `goal_type` TEXT NOT NULL,
  `priority` TEXT NOT NULL,
  `target_amount` DOUBLE,
  `target_date` DATE NOT NULL,
  `inflation_pct` DOUBLE,
  `current_amount` DOUBLE,
  `linked_json` TEXT,
  `monthly_contribution` DOUBLE,
  `step_up_pct` DOUBLE,
  `expected_return_pct` DOUBLE,
  `volatility_pct` DOUBLE,
  `retirement_monthly_expense` DOUBLE,
  `years_in_retirement` DOUBLE,
  `post_retirement_return_pct` DOUBLE,
  `emergency_months` DOUBLE,
  `notes` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  PRIMARY KEY (`goal_id`),
  KEY `idx_wealth_goal_owner` (`tenant_id`, `owner_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_goal_event`;
CREATE TABLE `atip_wealth_goal_event` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `goal_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `owner_id` TEXT NOT NULL,
  `at` DATETIME(6),
  `kind` TEXT NOT NULL,
  `details_json` TEXT,
  `actor` TEXT,
  PRIMARY KEY (`id`),
  KEY `idx_wealth_goal_event` (`goal_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_goal_projection`;
CREATE TABLE `atip_wealth_goal_projection` (
  `goal_id` VARCHAR(191) NOT NULL,
  `tenant_id` TEXT NOT NULL,
  `owner_id` TEXT NOT NULL,
  `as_of` DATE NOT NULL,
  `status` TEXT,
  `success_probability` DOUBLE,
  `projected` DOUBLE,
  `future_target` DOUBLE,
  `gap` DOUBLE,
  `result_json` TEXT,
  `methodology_version` TEXT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`goal_id`, `as_of`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_holding`;
CREATE TABLE `atip_wealth_holding` (
  `holding_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `asset_class` TEXT NOT NULL,
  `instrument` TEXT NOT NULL,
  `valuation` TEXT NOT NULL,
  `name` TEXT NOT NULL,
  `symbol` TEXT,
  `quantity` DOUBLE NOT NULL,
  `unit` TEXT,
  `avg_cost` DOUBLE,
  `currency` TEXT,
  `fx_rate` DOUBLE,
  `manual_price` DOUBLE,
  `manual_price_as_of` DATE,
  `maturity_date` DATE,
  `coupon_pct` DOUBLE,
  `notes` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `created_at` DATETIME(6),
  `updated_at` DATETIME(6),
  `updated_by` TEXT,
  PRIMARY KEY (`holding_id`),
  KEY `idx_wealth_holding_owner` (`tenant_id`, `owner_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_liability`;
CREATE TABLE `atip_wealth_liability` (
  `liability_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `kind` TEXT NOT NULL,
  `name` TEXT,
  `outstanding` DOUBLE NOT NULL,
  `interest_pct` DOUBLE,
  `emi` DOUBLE,
  `end_date` DATE,
  `notes` TEXT,
  `status` VARCHAR(191) NOT NULL,
  `updated_at` DATETIME(6),
  `updated_by` TEXT,
  PRIMARY KEY (`liability_id`),
  KEY `idx_wealth_liability_owner` (`tenant_id`, `owner_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_preference`;
CREATE TABLE `atip_wealth_preference` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `key` VARCHAR(191) NOT NULL,
  `value` TEXT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `owner_id`, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_rebalance_plan`;
CREATE TABLE `atip_wealth_rebalance_plan` (
  `plan_id` VARCHAR(191) NOT NULL,
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `created_at` DATETIME(6),
  `created_by` TEXT,
  `mode` TEXT,
  `new_cash` DOUBLE,
  `target_run_id` TEXT,
  `verdict` TEXT,
  `status` TEXT NOT NULL,
  `result_json` TEXT,
  `methodology_version` TEXT,
  `decided_at` DATETIME(6),
  `decided_by` TEXT,
  `decision_note` TEXT,
  PRIMARY KEY (`plan_id`),
  KEY `idx_wealth_rbl_owner` (`tenant_id`, `owner_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_wealth_snapshot`;
CREATE TABLE `atip_wealth_snapshot` (
  `tenant_id` VARCHAR(191) NOT NULL,
  `owner_id` VARCHAR(191) NOT NULL,
  `date` DATE NOT NULL,
  `net_worth` DOUBLE,
  `gross_assets` DOUBLE,
  `liabilities` DOUBLE,
  `invested_cost` DOUBLE,
  `by_class_json` TEXT,
  `positions` BIGINT,
  `created_at` DATETIME(6),
  PRIMARY KEY (`tenant_id`, `owner_id`, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;

DROP TABLE IF EXISTS `atip_weight_config`;
CREATE TABLE `atip_weight_config` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `index_name` VARCHAR(32) NOT NULL,
  `variable` VARCHAR(32) NOT NULL,
  `weight` DOUBLE NOT NULL,
  `description` TEXT,
  `regime` VARCHAR(32),
  `active` BIGINT,
  `updated_at` DATETIME(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_weight_config_index_name_variable_regime` (`index_name`, `variable`, `regime`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC;


COMMIT;
SET UNIQUE_CHECKS=1;
SET FOREIGN_KEY_CHECKS=1;

-- ATIP export (20261003-135620) part data
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS=0;
SET UNIQUE_CHECKS=0;
SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';
SET AUTOCOMMIT=0;
START TRANSACTION;

INSERT INTO `atip_schema_migrations` (`version`, `name`, `checksum`, `applied_at`, `duration_ms`, `status`, `rollback_note`, `error`) VALUES
('0001','baseline_w1_w7','85dba27c6c55c29b14c1a83e7e96727d7d4891c8b28bcd7f34e208dbc1e6def8','2026-10-03 13:53:52.060404',0.3,'APPLIED','none (validation only)',NULL),
('0002','w8_ops_tables','423f670c988eebcc1b4bd6d81be6618af654aced692695be717b4092837cec94','2026-10-03 13:53:52.062163',0.4,'APPLIED','DROP the W8 ops_* tables, enterprise_refresh_token and schema_migrations -- or restore the pre-W8 backup',NULL),
('0003','w8_indexes','5e9658c0054f9582e1cfb953987250af32e803b21165114d4e31d251cae3278c','2026-10-03 13:53:52.062998',0.3,'APPLIED','DROP INDEX idx_prices_date, idx_lq_timestamp, idx_pipeline_log_status, idx_ent_audit_at',NULL),
('0004','w8_audit_append_only','11070a711d294a0eb92a76b0eeef9a9270cd4de983641116e2da8665ed77deb0','2026-10-03 13:53:52.064009',0.4,'APPLIED','DROP TRIGGER trg_ent_audit_no_update, trg_ent_audit_no_delete, trg_oms_event_no_update, trg_oms_event_no_delete',NULL),
('0005','w20_wealth_append_only','6fde4b5ca4bbad3f0568e06fb4866bd31b90c97792d891be187d5866f715f18c','2026-10-03 13:53:52.065896',1.1,'APPLIED','DROP TRIGGER trg_<table>_no_update / trg_<table>_no_delete for investor_profile_version, perf_ledger, perf_ledger_void, perf_report_run, wealth_allocation_run, wealth_goal_event; the wealth tables themselves are additive (restore the pre-W20 backup to remove them)',NULL);
INSERT INTO `atip_weight_config` (`id`, `index_name`, `variable`, `weight`, `description`, `regime`, `active`, `updated_at`) VALUES
(1,'VPI','V',0.18,'Volatility (ATR%)','ALL',1,'2026-10-03 13:53:52'),
(2,'VPI','TS',0.15,'Trend Strength (ADX)','ALL',1,'2026-10-03 13:53:52'),
(3,'VPI','RS',0.12,'Relative Strength vs Nifty','ALL',1,'2026-10-03 13:53:52'),
(4,'VPI','LQ',0.1,'Liquidity','ALL',1,'2026-10-03 13:53:52'),
(5,'VPI','VOL',0.1,'Volume Expansion','ALL',1,'2026-10-03 13:53:52'),
(6,'VPI','MR',0.1,'Mean Reversion','ALL',1,'2026-10-03 13:53:52'),
(7,'VPI','FG',0.1,'Fundamental Growth','ALL',1,'2026-10-03 13:53:52'),
(8,'VPI','IS',0.08,'Institutional Strength','ALL',1,'2026-10-03 13:53:52'),
(9,'VPI','NS',0.07,'News Sentiment','ALL',1,'2026-10-03 13:53:52'),
(10,'SPI','ROE',0.2,'Return on Equity','ALL',1,'2026-10-03 13:53:52'),
(11,'SPI','ROCE',0.15,'Return on Capital','ALL',1,'2026-10-03 13:53:52'),
(12,'SPI','EPS',0.15,'EPS Growth YoY','ALL',1,'2026-10-03 13:53:52'),
(13,'SPI','Revenue',0.1,'Revenue Growth','ALL',1,'2026-10-03 13:53:52'),
(14,'SPI','FCF',0.1,'Free Cash Flow','ALL',1,'2026-10-03 13:53:52'),
(15,'SPI','Debt',0.1,'Debt/Equity inv','ALL',1,'2026-10-03 13:53:52'),
(16,'SPI','PEG',0.1,'PEG Ratio inv','ALL',1,'2026-10-03 13:53:52'),
(17,'SPI','Quality',0.1,'Quality Composite','ALL',1,'2026-10-03 13:53:52'),
(18,'RRI','Recovery',0.25,'Rebound from 52W low','ALL',1,'2026-10-03 13:53:52'),
(19,'RRI','Support',0.2,'Near support','ALL',1,'2026-10-03 13:53:52'),
(20,'RRI','Volume',0.15,'Vol on up-days','ALL',1,'2026-10-03 13:53:52'),
(21,'RRI','RSIRecovery',0.15,'RSI crossed 30','ALL',1,'2026-10-03 13:53:52'),
(22,'RRI','Institutional',0.15,'DII/MF buying','ALL',1,'2026-10-03 13:53:52'),
(23,'RRI','News',0.1,'Positive news','ALL',1,'2026-10-03 13:53:52'),
(24,'MRI','MACD',0.25,'MACD cross zero','ALL',1,'2026-10-03 13:53:52'),
(25,'MRI','RSI',0.2,'RSI cross 40','ALL',1,'2026-10-03 13:53:52'),
(26,'MRI','ADX',0.15,'ADX falling inv','ALL',1,'2026-10-03 13:53:52'),
(27,'MRI','Volume',0.15,'Vol surge','ALL',1,'2026-10-03 13:53:52'),
(28,'MRI','EMA',0.15,'9-EMA cross 21','ALL',1,'2026-10-03 13:53:52'),
(29,'MRI','News',0.1,'Catalyst news','ALL',1,'2026-10-03 13:53:52'),
(30,'CRI','Volatility',0.2,'High ATR near 52W high','ALL',1,'2026-10-03 13:53:52'),
(31,'CRI','Debt',0.2,'High D/E','ALL',1,'2026-10-03 13:53:52'),
(32,'CRI','Distribution',0.15,'Vol on down-days','ALL',1,'2026-10-03 13:53:52'),
(33,'CRI','WeakTrend',0.15,'Below DMAs','ALL',1,'2026-10-03 13:53:52'),
(34,'CRI','NegativeNews',0.15,'Negative news','ALL',1,'2026-10-03 13:53:52'),
(35,'CRI','MarketWeakness',0.15,'Sector+VIX','ALL',1,'2026-10-03 13:53:52'),
(36,'MSI','News',0.3,'Aggregate news','ALL',1,'2026-10-03 13:53:52'),
(37,'MSI','FII',0.2,'FII 5-day trend','ALL',1,'2026-10-03 13:53:52'),
(38,'MSI','DII',0.15,'DII activity','ALL',1,'2026-10-03 13:53:52'),
(39,'MSI','Sector',0.1,'% sectors green','ALL',1,'2026-10-03 13:53:52'),
(40,'MSI','Options',0.1,'PCR inverted','ALL',1,'2026-10-03 13:53:52'),
(41,'MSI','Global',0.1,'Global sentiment','ALL',1,'2026-10-03 13:53:52'),
(42,'MSI','VIX',0.05,'VIX inverted','ALL',1,'2026-10-03 13:53:52'),
(43,'ZPI','Support',0.15,'Near key support','ALL',1,'2026-10-03 13:53:52'),
(44,'ZPI','Resistance',0.15,'Room to run','ALL',1,'2026-10-03 13:53:52'),
(45,'ZPI','RSI',0.1,'RSI 30-50 zone','ALL',1,'2026-10-03 13:53:52'),
(46,'ZPI','ATR',0.1,'R:R >= 1:3','ALL',1,'2026-10-03 13:53:52'),
(47,'ZPI','Volume',0.1,'Low vol pullback','ALL',1,'2026-10-03 13:53:52'),
(48,'ZPI','Trend',0.1,'Above 200-DMA','ALL',1,'2026-10-03 13:53:52'),
(49,'ZPI','Institutional',0.1,'Accumulation 10d','ALL',1,'2026-10-03 13:53:52'),
(50,'ZPI','News',0.1,'No negative news','ALL',1,'2026-10-03 13:53:52'),
(51,'ZPI','Sector',0.1,'Top-3 sector','ALL',1,'2026-10-03 13:53:52'),
(52,'ACS','HistoricalAccuracy',0.25,'Past accuracy 90d','ALL',1,'2026-10-03 13:53:52'),
(53,'ACS','Agreement',0.2,'% indexes agree','ALL',1,'2026-10-03 13:53:52'),
(54,'ACS','MarketRegime',0.2,'MH>60','ALL',1,'2026-10-03 13:53:52'),
(55,'ACS','DataQuality',0.15,'Input completeness','ALL',1,'2026-10-03 13:53:52'),
(56,'ACS','NewsConfidence',0.1,'AI news conf','ALL',1,'2026-10-03 13:53:52'),
(57,'ACS','Liquidity',0.1,'Turnover','ALL',1,'2026-10-03 13:53:52'),
(58,'MH','NiftyTrend',0.2,'Nifty trend','ALL',1,'2026-10-03 13:53:52'),
(59,'MH','BankNifty',0.15,'BankNifty','ALL',1,'2026-10-03 13:53:52'),
(60,'MH','Breadth',0.1,'% above 200DMA','ALL',1,'2026-10-03 13:53:52'),
(61,'MH','VIX',0.1,'VIX inv','ALL',1,'2026-10-03 13:53:52'),
(62,'MH','FII',0.1,'FII net 5d','ALL',1,'2026-10-03 13:53:52'),
(63,'MH','DII',0.1,'DII net','ALL',1,'2026-10-03 13:53:52'),
(64,'MH','Global',0.1,'Global score','ALL',1,'2026-10-03 13:53:52'),
(65,'MH','Sector',0.1,'Sector breadth','ALL',1,'2026-10-03 13:53:52'),
(66,'MH','AdvanceDecline',0.05,'A/D ratio','ALL',1,'2026-10-03 13:53:52'),
(67,'ATIP','VPI',0.2,'VPI','ALL',1,'2026-10-03 13:53:52'),
(68,'ATIP','SPI',0.15,'SPI','ALL',1,'2026-10-03 13:53:52'),
(69,'ATIP','RRI',0.1,'RRI','ALL',1,'2026-10-03 13:53:52'),
(70,'ATIP','MRI',0.1,'MRI','ALL',1,'2026-10-03 13:53:52'),
(71,'ATIP','MSI',0.1,'MSI','ALL',1,'2026-10-03 13:53:52'),
(72,'ATIP','ZPI',0.1,'ZPI','ALL',1,'2026-10-03 13:53:52'),
(73,'ATIP','TS',0.1,'Tech Score','ALL',1,'2026-10-03 13:53:52'),
(74,'ATIP','FS',0.1,'Fund Score','ALL',1,'2026-10-03 13:53:52'),
(75,'ATIP','INS',0.05,'Inst Score','ALL',1,'2026-10-03 13:53:52'),
(76,'TOD','VPI',0.2,'VPI','ALL',1,'2026-10-03 13:53:52'),
(77,'TOD','ZPI',0.15,'ZPI','ALL',1,'2026-10-03 13:53:52'),
(78,'TOD','MRI',0.15,'MRI','ALL',1,'2026-10-03 13:53:52'),
(79,'TOD','MSI',0.1,'MSI','ALL',1,'2026-10-03 13:53:52'),
(80,'TOD','Volume',0.1,'Volume breakout','ALL',1,'2026-10-03 13:53:52'),
(81,'TOD','Breakout',0.1,'Price breakout','ALL',1,'2026-10-03 13:53:52'),
(82,'TOD','Sector',0.1,'Sector strength','ALL',1,'2026-10-03 13:53:52'),
(83,'TOD','ACS',0.1,'ACS','ALL',1,'2026-10-03 13:53:52'),
(84,'INS','FII',0.4,'Market FII 5-day net flow','ALL',1,'2026-10-03 13:53:52'),
(85,'INS','DII',0.3,'Market DII 5-day net flow','ALL',1,'2026-10-03 13:53:52'),
(86,'INS','Promoter',0.2,'Promoter shareholding %','ALL',1,'2026-10-03 13:53:52'),
(87,'INS','BulkDeals',0.1,'Net bulk/block deal value (10d)','ALL',1,'2026-10-03 13:53:52'),
(88,'TS','RSI',0.1,'RSI zone','ALL',1,'2026-10-03 13:53:52'),
(89,'TS','MACD',0.1,'MACD histogram','ALL',1,'2026-10-03 13:53:52'),
(90,'TS','ADX',0.1,'Trend strength','ALL',1,'2026-10-03 13:53:52'),
(91,'TS','ATR',0.08,'Volatility','ALL',1,'2026-10-03 13:53:52'),
(92,'TS','EMA',0.08,'EMA alignment','ALL',1,'2026-10-03 13:53:52'),
(93,'TS','VWAP',0.08,'VWAP (needs intraday)','ALL',1,'2026-10-03 13:53:52'),
(94,'TS','Bollinger',0.08,'Position in band','ALL',1,'2026-10-03 13:53:52'),
(95,'TS','Volume',0.08,'Volume ratio','ALL',1,'2026-10-03 13:53:52'),
(96,'TS','Trend',0.08,'Above 200-DMA','ALL',1,'2026-10-03 13:53:52'),
(97,'TS','SR',0.08,'Support/Resistance crosses','ALL',1,'2026-10-03 13:53:52'),
(98,'TS','Gap',0.07,'Gap analysis','ALL',1,'2026-10-03 13:53:52'),
(99,'TS','RelativeVolume',0.07,'Vs same-weekday avg','ALL',1,'2026-10-03 13:53:52'),
(100,'PHS','Diversification',0.25,'Concentration across holdings','ALL',1,'2026-10-03 13:53:52'),
(101,'PHS','Risk',0.2,'Portfolio beta + CRI exposure','ALL',1,'2026-10-03 13:53:52'),
(102,'PHS','Drawdown',0.15,'Drawdown from peak','ALL',1,'2026-10-03 13:53:52'),
(103,'PHS','Quality',0.15,'Mean ATIP score of holdings','ALL',1,'2026-10-03 13:53:52'),
(104,'PHS','Allocation',0.15,'Position sizing vs regime','ALL',1,'2026-10-03 13:53:52'),
(105,'PHS','Performance',0.1,'Unrealised P&L','ALL',1,'2026-10-03 13:53:52');

COMMIT;
SET UNIQUE_CHECKS=1;
SET FOREIGN_KEY_CHECKS=1;
