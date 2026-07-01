-- Migration: 0005_context_conflicts.sql
CREATE TABLE IF NOT EXISTS context_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    winning_device_id TEXT NOT NULL,
    losing_device_id TEXT NOT NULL,
    conflict_details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
