-- Migration: 0004_router_logs.sql
CREATE TABLE IF NOT EXISTS router_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    path TEXT NOT NULL,
    confidence REAL NOT NULL,
    latency_ms REAL NOT NULL,
    sentiment TEXT,
    success INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
