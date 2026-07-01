import os
import sqlite3
import json
import time
import queue
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("nova.db")

class TraceLogger:
    @staticmethod
    def log_query(sql, duration, params=None):
        logger.info(f"[DB TRACE] Query executed in {duration:.4f}s: {sql} | params: {params}")

# Thread-safe SQLite pool
class SQLitePool:
    def __init__(self, db_path, max_connections=5):
        self.db_path = db_path
        self.max_connections = max_connections
        self.pool = queue.Queue(max_connections)
        self.lock = threading.Lock()
        self.created = 0

    def get_connection(self):
        with self.lock:
            if self.pool.empty() and self.created < self.max_connections:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                # Optimize SQLite write speed and avoid concurrent write locks using WAL
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                except Exception:
                    pass
                self.created += 1
                return conn
        return self.pool.get(timeout=5.0)

    def put_connection(self, conn):
        try:
            self.pool.put_nowait(conn)
        except queue.Full:
            conn.close()
            with self.lock:
                self.created -= 1

    def close_all(self):
        with self.lock:
            while not self.pool.empty():
                try:
                    conn = self.pool.get_nowait()
                    conn.close()
                except Exception:
                    pass
            self.created = 0

class PGConnectionWrapper:
    def __init__(self, conn, store):
        self.conn = conn
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            if self.store.pg_pool:
                self.store.pg_pool.putconn(self.conn)
            else:
                self.conn.close()

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        cursor = self.conn.cursor()
        
        def run():
            start = time.perf_counter()
            cursor.execute(sql, params or ())
            duration = time.perf_counter() - start
            self.store._record_metrics(sql, duration)
            TraceLogger.log_query(sql, duration, params)
            
        self.store._execute_with_retry(run)
        return PGCursorWrapper(cursor)

    def executescript(self, sql_script):
        cursor = self.conn.cursor()
        
        def run():
            start = time.perf_counter()
            cursor.execute(sql_script)
            duration = time.perf_counter() - start
            self.store._record_metrics(sql_script, duration)
            TraceLogger.log_query(sql_script, duration)
            
        self.store._execute_with_retry(run)

class PGCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        try:
            row = self.cursor.fetchone()
        except Exception:
            return None
        if row is None:
            return None
        colnames = [desc[0] for desc in self.cursor.description]
        return dict(zip(colnames, row))

    def fetchall(self):
        try:
            rows = self.cursor.fetchall()
        except Exception:
            return []
        if not rows:
            return []
        colnames = [desc[0] for desc in self.cursor.description]
        return [dict(zip(colnames, row)) for row in rows]

class SQLiteConnectionWrapper:
    def __init__(self, conn, store):
        self.conn = conn
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            if self.store.sqlite_pool:
                self.store.sqlite_pool.put_connection(self.conn)
            else:
                self.conn.close()

    def execute(self, sql, params=None):
        def run():
            start = time.perf_counter()
            if params is not None:
                res = self.conn.execute(sql, params)
            else:
                res = self.conn.execute(sql)
            duration = time.perf_counter() - start
            self.store._record_metrics(sql, duration)
            TraceLogger.log_query(sql, duration, params)
            return res
            
        return self.store._execute_with_retry(run)

    def executescript(self, sql_script):
        def run():
            start = time.perf_counter()
            self.conn.executescript(sql_script)
            duration = time.perf_counter() - start
            self.store._record_metrics(sql_script, duration)
            TraceLogger.log_query(sql_script, duration)
            
        self.store._execute_with_retry(run)

class DatabaseStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path_param = db_path
        self.sqlite_pool = None
        self.pg_pool = None
        
        # Metrics trackers
        self.query_count = 0
        self.total_query_time = 0.0
        self.slowest_query_sql = ""
        self.slowest_query_time = 0.0
        self.metrics_lock = threading.Lock()

        self._configure_db()
        self.init_db()

    def _configure_db(self):
        self.db_type = os.getenv("DB_TYPE", "sqlite").lower()
        self.db_url = os.getenv("DATABASE_URL", "")

        if self.sqlite_pool:
            self.sqlite_pool.close_all()
            self.sqlite_pool = None
        if self.pg_pool:
            try:
                self.pg_pool.closeall()
            except Exception:
                pass
            self.pg_pool = None

        if self.db_type == "sqlite":
            if self.db_path_param is None:
                self.db_path = Path(__file__).parent.parent.parent / "nova.db"
            else:
                self.db_path = Path(self.db_path_param)
            self.sqlite_pool = SQLitePool(self.db_path, max_connections=5)
        else:
            self.db_path = None
            from psycopg2.pool import ThreadedConnectionPool
            self.pg_pool = ThreadedConnectionPool(1, 5, self.db_url)

    def switch_profile(self, db_type: str, db_url: str = "", db_path: str = None) -> None:
        """Dynamically switch profiles and re-initialize the pool."""
        logger.info(f"Switching database profile dynamically to {db_type}")
        os.environ["DB_TYPE"] = db_type
        if db_url:
            os.environ["DATABASE_URL"] = db_url
        if db_path:
            self.db_path_param = db_path
        self._configure_db()
        self.init_db()

    def get_connection(self):
        if self.db_type == "postgresql":
            conn = self.pg_pool.getconn()
            return PGConnectionWrapper(conn, self)
        else:
            conn = self.sqlite_pool.get_connection()
            return SQLiteConnectionWrapper(conn, self)

    def init_db(self):
        if self.db_type == "sqlite":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            
        # Ensure schema_migrations version tracking table exists
        with self.get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """)

        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        with self.get_connection() as conn:
            conn.executescript(schema_sql)

        # Run migrations
        migrations_dir = Path(__file__).parent / "migrations"
        if migrations_dir.exists():
            for migration_file in sorted(migrations_dir.glob("*.sql")):
                if ".rollback.sql" in migration_file.name:
                    continue
                version = migration_file.stem
                
                # Check if already applied
                sql_check = "SELECT 1 FROM schema_migrations WHERE version = ?"
                with self.get_connection() as conn:
                    row = conn.execute(sql_check, (version,)).fetchone()
                    if row:
                        continue

                logger.info(f"Applying migration: {version}")
                with open(migration_file, "r", encoding="utf-8") as f:
                    migration_sql = f.read()
                with self.get_connection() as conn:
                    conn.executescript(migration_sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, datetime.now(timezone.utc).isoformat())
                    )

    def rollback_migration(self, version: str) -> bool:
        """Runs the rollback script for a migration if it exists."""
        migrations_dir = Path(__file__).parent / "migrations"
        rollback_file = migrations_dir / f"{version}.rollback.sql"
        if not rollback_file.exists():
            logger.error(f"Rollback script for {version} not found at {rollback_file}")
            return False
            
        with open(rollback_file, "r", encoding="utf-8") as f:
            rollback_sql = f.read()
            
        with self.get_connection() as conn:
            conn.executescript(rollback_sql)
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
            
        logger.info(f"Successfully rolled back migration version: {version}")
        return True

    def validate_database_integrity(self) -> dict:
        """Validates schema tables presence and SQLite corruption states."""
        try:
            status = "healthy"
            details = []
            
            if self.db_type == "sqlite":
                with self.get_connection() as conn:
                    row = conn.execute("PRAGMA integrity_check;").fetchone()
                    if row and list(row)[0].lower() != "ok":
                        status = "corrupt"
                        details.append(f"Integrity check failed: {list(row)[0]}")
                        
            # Assert essential tables exist
            essential_tables = ["devices", "commands", "actions", "automation_rules", "schema_migrations"]
            for table in essential_tables:
                sql = ""
                if self.db_type == "sqlite":
                    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
                else:
                    sql = "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name=?"
                
                with self.get_connection() as conn:
                    row = conn.execute(sql, (table,)).fetchone()
                    if not row:
                        status = "incomplete"
                        details.append(f"Missing core table: {table}")
                        
            return {
                "success": status == "healthy",
                "status": status,
                "details": details if details else ["All database integrity checks passed."]
            }
        except Exception as e:
            return {
                "success": False,
                "status": "error",
                "details": [str(e)]
            }

    def export_data_to_json(self, export_path: Path | str) -> dict:
        """Exports all core table rows to a structured JSON file."""
        tables = ["devices", "commands", "actions", "automation_rules", "schema_migrations", "router_performance", "context_conflicts"]
        export_data = {}
        
        try:
            for table in tables:
                sql = f"SELECT * FROM {table}"
                with self.get_connection() as conn:
                    rows = conn.execute(sql).fetchall()
                    export_data[table] = [dict(row) for row in rows]
                    
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, indent=2)
                
            return {"success": True, "path": str(export_path), "tables": list(export_data.keys())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def import_data_from_json(self, import_path: Path | str) -> dict:
        """Imports tables from a structured JSON file."""
        try:
            with open(import_path, "r", encoding="utf-8") as f:
                import_data = json.load(f)
                
            # Truncate tables first to seed clean fixtures
            for table, rows in import_data.items():
                if table not in ["devices", "commands", "actions", "automation_rules", "schema_migrations", "router_performance", "context_conflicts"]:
                    continue
                with self.get_connection() as conn:
                    conn.execute(f"DELETE FROM {table}")
                    
                # Insert rows
                if not rows:
                    continue
                columns = rows[0].keys()
                placeholders = ", ".join(["?" for _ in columns])
                col_names = ", ".join(columns)
                sql_insert = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
                
                with self.get_connection() as conn:
                    for row in rows:
                        vals = [row[col] for col in columns]
                        conn.execute(sql_insert, vals)
                        
            return {"success": True, "imported_tables": list(import_data.keys())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def vacuum_database(self) -> dict:
        """Performs structural optimization vacuum runs."""
        try:
            start = time.perf_counter()
            with self.get_connection() as conn:
                if self.db_type == "sqlite":
                    conn.execute("VACUUM;")
                else:
                    conn.executescript("VACUUM ANALYZE;")
            duration = time.perf_counter() - start
            return {"success": True, "duration_seconds": round(duration, 4)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_metrics(self) -> dict:
        with self.metrics_lock:
            avg_time = (self.total_query_time / self.query_count) if self.query_count > 0 else 0.0
            return {
                "db_type": self.db_type,
                "query_count": self.query_count,
                "total_query_time_seconds": round(self.total_query_time, 4),
                "avg_query_time_seconds": round(avg_time, 4),
                "slowest_query_sql": self.slowest_query_sql,
                "slowest_query_time_seconds": round(self.slowest_query_time, 4),
                "pool_stats": {
                    "max_connections": 5,
                    "active_connections": self.sqlite_pool.created if self.sqlite_pool else 5
                }
            }

    def _record_metrics(self, sql: str, duration: float):
        with self.metrics_lock:
            self.query_count += 1
            self.total_query_time += duration
            if duration > self.slowest_query_time:
                self.slowest_query_time = duration
                self.slowest_query_sql = sql

    def _execute_with_retry(self, func, max_retries=3, delay=0.1):
        last_ex = None
        for attempt in range(max_retries):
            try:
                return func()
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                # Catch lock failures
                if "locked" in str(e) or "busy" in str(e):
                    last_ex = e
                    time.sleep(delay * (2 ** attempt))
                    continue
                raise e
            except Exception as e:
                # Network reconnect retries
                last_ex = e
                time.sleep(delay * (2 ** attempt))
                continue
        raise last_ex

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

    def log_router_performance(self, command: str, path: str, confidence: float, latency_ms: float, sentiment: str, success: bool) -> None:
        sql = """
        INSERT INTO router_performance (command, path, confidence, latency_ms, sentiment, success, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(sql, (
                command,
                path,
                confidence,
                latency_ms,
                sentiment,
                1 if success else 0,
                now
            ))

    def get_router_performance_metrics(self) -> dict:
        """Calculates total latency analytics and router accuracies."""
        sql = """
        SELECT COUNT(*) as cnt, AVG(latency_ms) as avg_latency, AVG(confidence) as avg_conf, SUM(success) as ok_cnt
        FROM router_performance
        """
        with self.get_connection() as conn:
            row = conn.execute(sql).fetchone()
            if row and row["cnt"]:
                return {
                    "count": row["cnt"],
                    "avg_latency_ms": round(row["avg_latency"], 2) if row["avg_latency"] else 0.0,
                    "avg_confidence": round(row["avg_conf"], 2) if row["avg_conf"] else 0.0,
                    "success_rate": round(row["ok_cnt"] / row["cnt"], 2) if row["cnt"] else 0.0
                }
        return {"count": 0, "avg_latency_ms": 0.0, "avg_confidence": 0.0, "success_rate": 0.0}

    def log_context_conflict(self, key: str, winning_device_id: str, losing_device_id: str, conflict_details: str) -> None:
        sql = """
        INSERT INTO context_conflicts (key, winning_device_id, losing_device_id, conflict_details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(sql, (key, winning_device_id, losing_device_id, conflict_details, now))

    def get_context_conflicts(self, limit: int = 20) -> list:
        sql = "SELECT * FROM context_conflicts ORDER BY created_at DESC LIMIT ?"
        with self.get_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
            return [dict(r) for r in rows]
