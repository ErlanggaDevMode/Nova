-- Rollback Migration: 0010_rule_chains.rollback.sql

DROP INDEX IF EXISTS idx_loop_breaker_alerts_created_at;
DROP INDEX IF EXISTS idx_loop_breaker_alerts_ack;
DROP INDEX IF EXISTS idx_loop_breaker_alerts_target;
DROP TABLE IF EXISTS loop_breaker_alerts;

DROP INDEX IF EXISTS idx_loop_breaker_states_state;
DROP TABLE IF EXISTS loop_breaker_states;

DROP INDEX IF EXISTS idx_rule_chain_executions_created_at;
DROP INDEX IF EXISTS idx_rule_chain_executions_status;
DROP INDEX IF EXISTS idx_rule_chain_executions_chain_id;
DROP TABLE IF EXISTS rule_chain_executions;

DROP INDEX IF EXISTS idx_rule_trigger_events_target_time;
DROP TABLE IF EXISTS rule_trigger_events;

DROP INDEX IF EXISTS idx_rule_trigger_policies_type;
DROP TABLE IF EXISTS rule_trigger_policies;

DROP INDEX IF EXISTS idx_rule_chain_links_target;
DROP INDEX IF EXISTS idx_rule_chain_links_source;
DROP INDEX IF EXISTS idx_rule_chain_links_pair;
DROP TABLE IF EXISTS rule_chain_links;

DROP INDEX IF EXISTS idx_rule_chains_enabled;
DROP TABLE IF EXISTS rule_chains;
