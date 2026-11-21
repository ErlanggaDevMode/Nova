-- Rollback Migration: 0012_home_assistant.rollback.sql

DROP INDEX IF EXISTS idx_ha_dispatch_log_entity;
DROP INDEX IF EXISTS idx_ha_dispatch_log_direction_created;
DROP TABLE IF EXISTS ha_dispatch_log;

DROP INDEX IF EXISTS idx_ha_entity_triggers_enabled_entity;
DROP TABLE IF EXISTS ha_entity_triggers;

DROP INDEX IF EXISTS idx_ha_entities_state;
DROP INDEX IF EXISTS idx_ha_entities_domain;
DROP TABLE IF EXISTS ha_entities;

DROP TABLE IF EXISTS ha_config;
