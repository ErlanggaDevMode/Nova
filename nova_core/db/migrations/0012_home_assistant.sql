-- Migration: 0012_home_assistant.sql
-- Phase 21 (F21.1–F21.5): Home Assistant Event Adapters, Entities, Capability Maps, Triggers & Dispatch Log

CREATE TABLE IF NOT EXISTS ha_config (
    id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL DEFAULT 'http://homeassistant.local:8123',
    ws_url TEXT,
    access_token TEXT NOT NULL DEFAULT '',
    verify_ssl INTEGER NOT NULL DEFAULT 1,
    timeout_seconds REAL NOT NULL DEFAULT 10.0,
    reconnect_base_delay REAL NOT NULL DEFAULT 1.0,
    reconnect_max_delay REAL NOT NULL DEFAULT 60.0,
    auto_subscribe_events TEXT NOT NULL DEFAULT '["state_changed"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ha_entities (
    entity_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    friendly_name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'unknown',
    attributes TEXT NOT NULL DEFAULT '{}',
    capabilities TEXT NOT NULL DEFAULT '{}',
    area_id TEXT,
    device_class TEXT,
    last_changed TEXT,
    last_updated TEXT,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ha_entities_domain
    ON ha_entities(domain);

CREATE INDEX IF NOT EXISTS idx_ha_entities_state
    ON ha_entities(state);

CREATE TABLE IF NOT EXISTS ha_entity_triggers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    entity_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    attribute_name TEXT,
    attribute_operator TEXT,
    attribute_value TEXT,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    event_type TEXT NOT NULL DEFAULT 'ha.state_trigger',
    description TEXT,
    last_triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ha_entity_triggers_enabled_entity
    ON ha_entity_triggers(enabled, entity_id);

CREATE TABLE IF NOT EXISTS ha_dispatch_log (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'internal',
    event_type TEXT NOT NULL,
    entity_id TEXT,
    domain TEXT,
    service TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ha_dispatch_log_direction_created
    ON ha_dispatch_log(direction, created_at);

CREATE INDEX IF NOT EXISTS idx_ha_dispatch_log_entity
    ON ha_dispatch_log(entity_id);
