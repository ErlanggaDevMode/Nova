-- Migration: 0006_execution_history.sql
CREATE TABLE IF NOT EXISTS execution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT,
    action_id TEXT,
    action_type TEXT NOT NULL,
    category TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    validated INTEGER NOT NULL DEFAULT 1,
    repaired INTEGER NOT NULL DEFAULT 0,
    params TEXT NOT NULL,
    result TEXT,
    error_message TEXT,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_history_created_at ON execution_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_history_action_type ON execution_history(action_type);
CREATE INDEX IF NOT EXISTS idx_execution_history_status ON execution_history(status);
