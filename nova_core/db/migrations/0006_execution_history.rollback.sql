-- Rollback Migration: 0006_execution_history.rollback.sql
DROP INDEX IF EXISTS idx_execution_history_status;
DROP INDEX IF EXISTS idx_execution_history_action_type;
DROP INDEX IF EXISTS idx_execution_history_created_at;
DROP TABLE IF EXISTS execution_history;
