import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

class PGConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, sql, params=None):
        # Translate SQLite ? placeholders to PostgreSQL %s placeholders
        sql = sql.replace("?", "%s")
        # Replace SQLite INSERT OR REPLACE with ON CONFLICT (if any)
        if "INSERT OR REPLACE" in sql:
            # We enforce standard ON CONFLICT instead of INSERT OR REPLACE in the stores directly,
            # but we can translate as a safety net if needed.
            pass
        cursor = self.conn.cursor()
        cursor.execute(sql, params or ())
        return PGCursorWrapper(cursor)

    def executescript(self, sql_script):
        cursor = self.conn.cursor()
        cursor.execute(sql_script)

class PGCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        colnames = [desc[0] for desc in self.cursor.description]
        return dict(zip(colnames, row))

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        colnames = [desc[0] for desc in self.cursor.description]
        return [dict(zip(colnames, row)) for row in rows]

class SQLiteConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, sql, params=None):
        if params is not None:
            return self.conn.execute(sql, params)
        return self.conn.execute(sql)

    def executescript(self, sql_script):
        self.conn.executescript(sql_script)

class DatabaseStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_type = os.getenv("DB_TYPE", "sqlite").lower()
        self.db_url = os.getenv("DATABASE_URL", "")

        if self.db_type == "sqlite":
            if db_path is None:
                db_path = Path(__file__).parent.parent.parent / "nova.db"
            self.db_path = Path(db_path)
        else:
            self.db_path = None

        self.init_db()

    def get_connection(self):
        if self.db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(self.db_url)
            return PGConnectionWrapper(conn)
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return SQLiteConnectionWrapper(conn)

    def init_db(self):
        if self.db_type == "sqlite":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        with self.get_connection() as conn:
            conn.executescript(schema_sql)

        # Run migrations
        migrations_dir = Path(__file__).parent / "migrations"
        if migrations_dir.exists():
            for migration_file in sorted(migrations_dir.glob("*.sql")):
                with open(migration_file, "r", encoding="utf-8") as f:
                    migration_sql = f.read()
                with self.get_connection() as conn:
                    conn.executescript(migration_sql)

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
