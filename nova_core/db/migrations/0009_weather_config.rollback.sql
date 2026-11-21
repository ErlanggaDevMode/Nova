-- Rollback Migration: 0009_weather_config.rollback.sql

DROP TABLE IF EXISTS weather_condition_state;

DROP INDEX IF EXISTS idx_weather_alert_rules_entity;
DROP INDEX IF EXISTS idx_weather_alert_rules_enabled_type;
DROP TABLE IF EXISTS weather_alert_rules;

DROP INDEX IF EXISTS idx_weather_locations_entity_type;
DROP TABLE IF EXISTS weather_locations;

DROP TABLE IF EXISTS weather_config;
