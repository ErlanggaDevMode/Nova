import json
from datetime import datetime, timezone
from typing import List, Optional
from nova_core.db.store import DatabaseStore

class ContextStore:
    def __init__(self, store: DatabaseStore):
        self.store = store

    def get_state(self, key: str) -> Optional[dict]:
        sql = "SELECT value FROM context_state WHERE key = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (key,)).fetchone()
            if row:
                return json.loads(row["value"])
        return None

    def set_state(self, key: str, value: dict, updated_by_device_id: str) -> None:
        sql = """
        INSERT INTO context_state (key, value, updated_by_device_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_by_device_id = excluded.updated_by_device_id,
            updated_at = excluded.updated_at
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.store.get_connection() as conn:
            conn.execute(sql, (key, json.dumps(value), updated_by_device_id, now))

    def delete_state(self, key: str) -> None:
        sql = "DELETE FROM context_state WHERE key = ?"
        with self.store.get_connection() as conn:
            conn.execute(sql, (key,))

    def list_states(self) -> List[dict]:
        sql = "SELECT * FROM context_state ORDER BY updated_at DESC"
        with self.store.get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            results = []
            for r in rows:
                results.append({
                    "key": r["key"],
                    "value": json.loads(r["value"]),
                    "updated_by_device_id": r["updated_by_device_id"],
                    "updated_at": r["updated_at"]
                })
            return results

    def get_system_prompt_context(self) -> str:
        """
        Formats active context states for inclusion in the LLM system prompt.
        """
        states = self.list_states()
        if not states:
            return ""

        lines = ["\n[ACTIVE TASK CONTEXT & SHORT-TERM MEMORY]"]
        for s in states:
            lines.append(f"- Key: {s['key']}")
            lines.append(f"  Value: {json.dumps(s['value'])}")
            lines.append(f"  Last Updated: {s['updated_at']}")
        lines.append("")
        return "\n".join(lines)
