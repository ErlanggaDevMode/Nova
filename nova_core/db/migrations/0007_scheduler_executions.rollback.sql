-- Rollback Migration: 0007_scheduler_executions.rollback.sql
DROP INDEX IF EXISTS idx_scheduler_executions_created_at;
DROP INDEX IF EXISTS idx_scheduler_executions_status;
DROP INDEX IF EXISTS idx_scheduler_executions_rule_id;
DROP INDEX IF EXISTS idx_scheduled_rules_enabled;
DROP TABLE IF EXISTS scheduler_executions;
DROP TABLE IF EXISTS scheduled_rules;
