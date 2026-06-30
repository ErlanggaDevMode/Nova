CREATE TABLE IF NOT EXISTS automation_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    condition TEXT NOT NULL,          -- JSON string
    action_template TEXT NOT NULL,    -- JSON string
    enabled INTEGER NOT NULL DEFAULT 1, -- boolean 0 or 1
    created_at TEXT NOT NULL          -- ISO timestamp
);
