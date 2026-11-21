-- Migration: 0007_scheduler_executions.sql
CREATE TABLE IF NOT EXISTS scheduled_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    action_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    prevent_overlap INTEGER NOT NULL DEFAULT 1,
    lock_ttl_seconds INTEGER NOT NULL DEFAULT 300,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    lock_token TEXT,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    result TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_rules_enabled ON scheduled_rules(enabled);
CREATE INDEX IF NOT EXISTS idx_scheduler_executions_rule_id ON scheduler_executions(rule_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_executions_status ON scheduler_executions(status);
CREATE INDEX IF NOT EXISTS idx_scheduler_executions_created_at ON scheduler_executions(created_at DESC);
