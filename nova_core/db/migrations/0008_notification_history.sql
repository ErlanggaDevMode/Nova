-- Migration: 0008_notification_history.sql
-- Phase 16 (F16.1–F16.5): Advanced Notification Rules, Silent Schedule, and Incoming Notification History

CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL UNIQUE,
    source_device_id TEXT NOT NULL DEFAULT 'unknown',
    package_name TEXT NOT NULL,
    sender TEXT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    alert_level TEXT NOT NULL,
    priority_score INTEGER NOT NULL DEFAULT 0,
    whitelisted INTEGER NOT NULL DEFAULT 1,
    silenced INTEGER NOT NULL DEFAULT 0,
    bypassed_silent INTEGER NOT NULL DEFAULT 0,
    deliver INTEGER NOT NULL DEFAULT 1,
    matched_rule_ids TEXT NOT NULL DEFAULT '[]',
    matched_rule_names TEXT NOT NULL DEFAULT '[]',
    reasons TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_history_created_at
    ON notification_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_history_package
    ON notification_history(package_name);
CREATE INDEX IF NOT EXISTS idx_notification_history_alert_level
    ON notification_history(alert_level);
CREATE INDEX IF NOT EXISTS idx_notification_history_silenced
    ON notification_history(silenced);

CREATE TABLE IF NOT EXISTS notification_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    package_patterns TEXT NOT NULL DEFAULT '[]',
    title_regex TEXT,
    body_regex TEXT,
    sender_regex TEXT,
    keywords TEXT NOT NULL DEFAULT '[]',
    regex_case_sensitive INTEGER NOT NULL DEFAULT 0,
    alert_level TEXT NOT NULL DEFAULT 'normal',
    priority_score INTEGER NOT NULL DEFAULT 50,
    bypass_silent INTEGER NOT NULL DEFAULT 0,
    stop_on_match INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_rules_enabled_priority
    ON notification_rules(enabled, priority_score DESC);

CREATE TABLE IF NOT EXISTS notification_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
