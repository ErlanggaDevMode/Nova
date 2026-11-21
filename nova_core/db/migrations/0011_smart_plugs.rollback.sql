-- Rollback Migration: 0011_smart_plugs.rollback.sql

DROP INDEX IF EXISTS idx_smart_plug_rules_category_room;
DROP INDEX IF EXISTS idx_smart_plug_rules_enabled;
DROP TABLE IF EXISTS smart_plug_rules;

DROP INDEX IF EXISTS idx_smart_plug_status_history_plug_synced;
DROP TABLE IF EXISTS smart_plug_status_history;

DROP INDEX IF EXISTS idx_smart_plugs_host_channel;
DROP INDEX IF EXISTS idx_smart_plugs_category_room;
DROP INDEX IF EXISTS idx_smart_plugs_provider;
DROP TABLE IF EXISTS smart_plugs;
