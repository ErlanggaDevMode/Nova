import json
import os
from datetime import datetime, timezone
from typing import List, Optional
from nova_core.db.store import DatabaseStore

class RulesStore:
    def __init__(self, store: DatabaseStore):
        self.store = store

    def create_rule(self, name: str, condition: dict, action_template: dict, enabled: int = 1) -> str:
        rule_id = os.urandom(8).hex()
        sql = """
        INSERT INTO automation_rules (id, name, condition, action_template, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.store.get_connection() as conn:
            conn.execute(sql, (
                rule_id,
                name,
                json.dumps(condition),
                json.dumps(action_template),
                enabled,
                now
            ))
        return rule_id

    def get_rule(self, rule_id: str) -> Optional[dict]:
        sql = "SELECT * FROM automation_rules WHERE id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (rule_id,)).fetchone()
            if row:
                res = dict(row)
                res["condition"] = json.loads(res["condition"])
                res["action_template"] = json.loads(res["action_template"])
                return res
        return None

    def list_rules(self) -> List[dict]:
        sql = "SELECT * FROM automation_rules ORDER BY created_at DESC"
        with self.store.get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            results = []
            for r in rows:
                res = dict(r)
                res["condition"] = json.loads(res["condition"])
                res["action_template"] = json.loads(res["action_template"])
                results.append(res)
            return results

    def update_rule(self, rule_id: str, name: Optional[str] = None, condition: Optional[dict] = None, action_template: Optional[dict] = None, enabled: Optional[int] = None) -> None:
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if condition is not None:
            updates.append("condition = ?")
            params.append(json.dumps(condition))
        if action_template is not None:
            updates.append("action_template = ?")
            params.append(json.dumps(action_template))
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(enabled)

        if not updates:
            return

        sql = f"UPDATE automation_rules SET {', '.join(updates)} WHERE id = ?"
        params.append(rule_id)

        with self.store.get_connection() as conn:
            conn.execute(sql, tuple(params))

    def delete_rule(self, rule_id: str) -> None:
        sql = "DELETE FROM automation_rules WHERE id = ?"
        with self.store.get_connection() as conn:
            conn.execute(sql, (rule_id,))
