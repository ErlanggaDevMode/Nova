-- Migration: 0011_smart_plugs.sql
-- Phase 20 (F20.1–F20.5): Smart Plug HTTP/Local Integrations (Shelly & Kasa), Status Telemetry & Category Rules

CREATE TABLE IF NOT EXISTS smart_plugs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER,
    protocol_version TEXT NOT NULL DEFAULT 'gen1',
    channel INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'general',
    room TEXT,
    auth_username TEXT,
    auth_password TEXT,
    auth_token TEXT,
    use_https INTEGER NOT NULL DEFAULT 0,
    timeout_seconds REAL NOT NULL DEFAULT 5.0,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_on INTEGER,
    power_watts REAL,
    voltage REAL,
    current_amps REAL,
    total_energy_wh REAL,
    online INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    last_error TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_smart_plugs_provider
    ON smart_plugs(provider);

CREATE INDEX IF NOT EXISTS idx_smart_plugs_category_room
    ON smart_plugs(category, room);

CREATE INDEX IF NOT EXISTS idx_smart_plugs_host_channel
    ON smart_plugs(host, channel);

CREATE TABLE IF NOT EXISTS smart_plug_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plug_id TEXT NOT NULL,
    online INTEGER NOT NULL DEFAULT 1,
    is_on INTEGER,
    power_watts REAL,
    voltage REAL,
    current_amps REAL,
    total_energy_wh REAL,
    temperature_c REAL,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_smart_plug_status_history_plug_synced
    ON smart_plug_status_history(plug_id, synced_at DESC);

CREATE TABLE IF NOT EXISTS smart_plug_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    category TEXT,
    room TEXT,
    plug_ids TEXT NOT NULL DEFAULT '[]',
    raw_rule TEXT NOT NULL,
    normalized_condition TEXT NOT NULL,
    normalized_action TEXT NOT NULL,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_smart_plug_rules_enabled
    ON smart_plug_rules(enabled);

CREATE INDEX IF NOT EXISTS idx_smart_plug_rules_category_room
    ON smart_plug_rules(category, room);
