-- Migration: 0009_weather_config.sql
-- Phase 17 (F17.1–F17.5): Weather API Configuration, Dynamic Locations, Alert Rules & Condition State

CREATE TABLE IF NOT EXISTS weather_config (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'openweathermap',
    api_key TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT 'https://api.openweathermap.org/data/2.5/weather',
    units TEXT NOT NULL DEFAULT 'metric',
    cache_ttl_seconds INTEGER NOT NULL DEFAULT 300,
    timeout_seconds REAL NOT NULL DEFAULT 10.0,
    default_city TEXT,
    default_lat REAL,
    default_lon REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_locations (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL DEFAULT 'device',
    city TEXT,
    lat REAL,
    lon REAL,
    label TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weather_locations_entity_type
    ON weather_locations(entity_type);

CREATE TABLE IF NOT EXISTS weather_alert_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    rule_type TEXT NOT NULL,
    threshold REAL,
    units TEXT NOT NULL DEFAULT 'metric',
    from_conditions TEXT NOT NULL DEFAULT '[]',
    to_conditions TEXT NOT NULL DEFAULT '[]',
    entity_id TEXT,
    cooldown_seconds INTEGER NOT NULL DEFAULT 600,
    severity TEXT NOT NULL DEFAULT 'warning',
    description TEXT,
    last_triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_weather_alert_rules_enabled_type
    ON weather_alert_rules(enabled, rule_type);

CREATE INDEX IF NOT EXISTS idx_weather_alert_rules_entity
    ON weather_alert_rules(entity_id);

CREATE TABLE IF NOT EXISTS weather_condition_state (
    location_key TEXT PRIMARY KEY,
    last_condition TEXT NOT NULL,
    last_temperature REAL,
    last_humidity REAL,
    observed_at TEXT NOT NULL
);
