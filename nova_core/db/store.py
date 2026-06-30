import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

class DatabaseStore:
    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            # Default to nova.db in the workspace root
            db_path = Path(__file__).parent.parent.parent / "nova.db"
        self.db_path = Path(db_path)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        with self.get_connection() as conn:
            conn.executescript(schema_sql)

    def register_device(self, device_id: str, name: str, platform: str, capabilities: dict) -> None:
        sql = """
        INSERT INTO devices (id, name, platform, capabilities, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            platform = excluded.platform,
            capabilities = excluded.capabilities,
            last_seen_at = excluded.last_seen_at
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(sql, (device_id, name, platform, json.dumps(capabilities), now))

    def get_device(self, device_id: str) -> dict | None:
        sql = "SELECT * FROM devices WHERE id = ?"
        with self.get_connection() as conn:
            row = conn.execute(sql, (device_id,)).fetchone()
            if row:
                res = dict(row)
                res["capabilities"] = json.loads(res["capabilities"])
                return res
        return None

    def log_command(self, command_id: str, raw_text: str, source_device_id: str, routed_path: str) -> None:
        sql = """
        INSERT INTO commands (id, raw_text, source_device_id, routed_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(sql, (command_id, raw_text, source_device_id, routed_path, now))

    def log_action(self, action_id: str, command_id: str | None, action_type: str, category: str, params: dict, permission_decision: dict, executed: bool = False, executed_at: str | None = None, result: dict | None = None) -> None:
        sql = """
        INSERT INTO actions (id, command_id, action_type, category, params, permission_decision, executed, executed_at, result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.get_connection() as conn:
            conn.execute(sql, (
                action_id,
                command_id,
                action_type,
                category,
                json.dumps(params),
                json.dumps(permission_decision),
                1 if executed else 0,
                executed_at,
                json.dumps(result) if result is not None else None
            ))

    def update_action_result(self, action_id: str, executed: bool, result: dict) -> None:
        sql = """
        UPDATE actions
        SET executed = ?, executed_at = ?, result = ?
        WHERE id = ?
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(sql, (
                1 if executed else 0,
                now,
                json.dumps(result),
                action_id
            ))
