-- Migration: 0010_rule_chains.sql
-- Phase 19 (F19.1–F19.5): Rule Chain & Loop Protections

CREATE TABLE IF NOT EXISTS rule_chains (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    steps TEXT NOT NULL DEFAULT '[]',
    max_depth INTEGER NOT NULL DEFAULT 10,
    stop_on_error INTEGER NOT NULL DEFAULT 1,
    trigger_limit INTEGER,
    trigger_window_seconds INTEGER NOT NULL DEFAULT 60,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    breaker_failure_threshold INTEGER NOT NULL DEFAULT 3,
    breaker_recovery_seconds INTEGER NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_chains_enabled
    ON rule_chains(enabled);

CREATE TABLE IF NOT EXISTS rule_chain_links (
    id TEXT PRIMARY KEY,
    source_rule_id TEXT NOT NULL,
    target_rule_id TEXT NOT NULL,
    condition TEXT NOT NULL DEFAULT 'on_success',
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rule_chain_links_pair
    ON rule_chain_links(source_rule_id, target_rule_id);
CREATE INDEX IF NOT EXISTS idx_rule_chain_links_source
    ON rule_chain_links(source_rule_id);
CREATE INDEX IF NOT EXISTS idx_rule_chain_links_target
    ON rule_chain_links(target_rule_id);

CREATE TABLE IF NOT EXISTS rule_trigger_policies (
    target_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL DEFAULT 'rule',
    max_triggers INTEGER NOT NULL DEFAULT 10,
    window_seconds INTEGER NOT NULL DEFAULT 60,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    last_triggered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_trigger_policies_type
    ON rule_trigger_policies(target_type);

CREATE TABLE IF NOT EXISTS rule_trigger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'rule',
    triggered_at TEXT NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_rule_trigger_events_target_time
    ON rule_trigger_events(target_id, triggered_at DESC);

CREATE TABLE IF NOT EXISTS rule_chain_executions (
    id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    chain_name TEXT NOT NULL,
    status TEXT NOT NULL,
    initial_context TEXT NOT NULL DEFAULT '{}',
    final_context TEXT NOT NULL DEFAULT '{}',
    step_results TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_chain_executions_chain_id
    ON rule_chain_executions(chain_id);
CREATE INDEX IF NOT EXISTS idx_rule_chain_executions_status
    ON rule_chain_executions(status);
CREATE INDEX IF NOT EXISTS idx_rule_chain_executions_created_at
    ON rule_chain_executions(created_at DESC);

CREATE TABLE IF NOT EXISTS loop_breaker_states (
    target_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL DEFAULT 'chain',
    state TEXT NOT NULL DEFAULT 'closed',
    failure_count INTEGER NOT NULL DEFAULT 0,
    loop_count INTEGER NOT NULL DEFAULT 0,
    failure_threshold INTEGER NOT NULL DEFAULT 3,
    recovery_timeout_seconds INTEGER NOT NULL DEFAULT 60,
    last_failure_at TEXT,
    opened_at TEXT,
    half_open_at TEXT,
    last_reset_at TEXT,
    last_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_loop_breaker_states_state
    ON loop_breaker_states(state);

CREATE TABLE IF NOT EXISTS loop_breaker_alerts (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'chain',
    severity TEXT NOT NULL DEFAULT 'critical',
    breaker_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_loop_breaker_alerts_target
    ON loop_breaker_alerts(target_id);
CREATE INDEX IF NOT EXISTS idx_loop_breaker_alerts_ack
    ON loop_breaker_alerts(acknowledged);
CREATE INDEX IF NOT EXISTS idx_loop_breaker_alerts_created_at
    ON loop_breaker_alerts(created_at DESC);
