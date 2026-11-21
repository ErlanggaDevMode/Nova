-- Rollback Migration: 0008_notification_history.rollback.sql

DROP TABLE IF EXISTS notification_settings;

DROP INDEX IF EXISTS idx_notification_rules_enabled_priority;
DROP TABLE IF EXISTS notification_rules;

DROP INDEX IF EXISTS idx_notification_history_silenced;
DROP INDEX IF EXISTS idx_notification_history_alert_level;
DROP INDEX IF EXISTS idx_notification_history_package;
DROP INDEX IF EXISTS idx_notification_history_created_at;
DROP TABLE IF EXISTS notification_history;
