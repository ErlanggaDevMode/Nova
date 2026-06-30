CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,         -- 'desktop' | 'android' | 'web'
    capabilities TEXT NOT NULL,     -- stored as JSON string
    last_seen_at TEXT NOT NULL      -- ISO timestamp
);

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    raw_text TEXT NOT NULL,
    source_device_id TEXT NOT NULL REFERENCES devices(id),
    routed_path TEXT NOT NULL,      -- 'local' | 'cloud'
    created_at TEXT NOT NULL        -- ISO timestamp
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    command_id TEXT REFERENCES commands(id),
    action_type TEXT NOT NULL,
    category TEXT NOT NULL,
    params TEXT NOT NULL,           -- stored as JSON string
    permission_decision TEXT NOT NULL, -- stored as JSON string
    executed INTEGER NOT NULL DEFAULT 0, -- boolean 0 or 1
    executed_at TEXT,               -- ISO timestamp
    result TEXT                     -- stored as JSON string
);
