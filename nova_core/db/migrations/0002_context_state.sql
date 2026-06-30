CREATE TABLE IF NOT EXISTS context_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,              -- JSON string
    updated_by_device_id TEXT REFERENCES devices(id),
    updated_at TEXT NOT NULL          -- ISO timestamp
);
