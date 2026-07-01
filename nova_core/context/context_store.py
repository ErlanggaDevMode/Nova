import json
from datetime import datetime, timezone
from typing import List, Optional
from nova_core.db.store import DatabaseStore

class ContextStore:
    def __init__(self, store: DatabaseStore, manager = None):
        self.store = store
        self.manager = manager
        
        # Priority mapping: 2 = High (keep), 1 = Normal (prunable)
        self.priorities = {
            "active_task": 2,
            "permissions": 2,
            "current_location": 2,
            "current_battery": 2,
            "history": 1,
            "playback_status": 1,
            "running_apps": 1
        }
        
        # Device priority mappings (High priority wins conflicts)
        self.device_priorities = {
            "desktop_agent": 3,
            "android_agent": 2,
            "web_dashboard": 1
        }
        
        # TTL seconds per priority level
        self.ttls = {
            2: 3600,  # 1 hour for high priority
            1: 1800   # 30 mins for normal priority
        }

    def _get_priority(self, key: str) -> int:
        return self.priorities.get(key, 1)

    def _truncate_lists(self, value: dict) -> dict:
        """Limit array/list context elements to max 5 items."""
        if not isinstance(value, dict):
            return value
        new_val = dict(value)
        for k, v in new_val.items():
            if isinstance(v, list):
                new_val[k] = v[-5:]  # Keep last 5 elements
        return new_val

    def _deep_merge(self, dict1: dict, dict2: dict) -> dict:
        """Deep merge two dictionaries."""
        res = dict(dict1)
        for k, v in dict2.items():
            if k in res and isinstance(res[k], dict) and isinstance(v, dict):
                res[k] = self._deep_merge(res[k], v)
            else:
                res[k] = v
        return res

    def _expire_stale_states(self) -> None:
        """Automatically delete expired states based on updated_at and TTL configs."""
        states = self.list_states()
        now = datetime.now(timezone.utc)
        for s in states:
            try:
                updated_at = datetime.fromisoformat(s["updated_at"].replace("Z", "+00:00"))
                age = (now - updated_at).total_seconds()
                priority = self._get_priority(s["key"])
                max_age = self.ttls.get(priority, 1800)
                if age > max_age:
                    self.delete_state(s["key"])
            except Exception:
                continue

    def get_state(self, key: str) -> Optional[dict]:
        self._expire_stale_states()
        sql = "SELECT value FROM context_state WHERE key = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (key,)).fetchone()
            if row:
                return json.loads(row["value"])
        return None

    def set_state(self, key: str, value: dict, updated_by_device_id: str) -> None:
        value = self._truncate_lists(value)
        
        # 1. Device Hierarchy Conflict Resolution
        sql_check = "SELECT value, updated_by_device_id, updated_at FROM context_state WHERE key = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql_check, (key,)).fetchone()
            
        if row:
            old_val = json.loads(row["value"])
            old_device = row["updated_by_device_id"]
            old_time_str = row["updated_at"]
            
            try:
                old_time = datetime.fromisoformat(old_time_str.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - old_time).total_seconds()
                
                # Check conflict window (5 seconds)
                if age < 5.0:
                    old_prio = self.device_priorities.get(old_device, 1)
                    new_prio = self.device_priorities.get(updated_by_device_id, 1)
                    
                    if new_prio < old_prio:
                        # Reject update and log sync conflict
                        details = f"Rejected update from '{updated_by_device_id}' (priority {new_prio}) in favor of '{old_device}' (priority {old_prio}) within conflict window."
                        self.store.log_context_conflict(key, old_device, updated_by_device_id, details)
                        import logging
                        logger = logging.getLogger("nova.context")
                        logger.warning(f"[CONTEXT SYNC CONFLICT] {details}")
                        return
            except Exception:
                pass
                
            # 2. Deep Merge scalar fields if both are dicts
            if isinstance(old_val, dict) and isinstance(value, dict):
                value = self._deep_merge(old_val, value)

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
            
        self._prune_context_window()
        
        # 3. Broadcast sync notifications over WebSocket
        if self.manager:
            import asyncio
            payload = {
                "event": "context.update",
                "key": key,
                "value": value,
                "updated_by": updated_by_device_id
            }
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.manager.broadcast(payload))
            except RuntimeError:
                # No event loop running (e.g. synchronous tests)
                coro = self.manager.broadcast(payload)
                if asyncio.iscoroutine(coro):
                    asyncio.run(coro)
            except Exception:
                pass

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

    def _prune_context_window(self) -> None:
        """Sliding window char count pruning targeting older, lower-priority states first."""
        budget = 1500
        states = self.list_states()
        
        current_len = len(self._format_context_prompt(states))
        if current_len <= budget:
            return
            
        normal_states = [s for s in states if self._get_priority(s["key"]) == 1]
        normal_states.sort(key=lambda x: x["updated_at"])  # Oldest first
        
        for s in normal_states:
            self.delete_state(s["key"])
            states = self.list_states()
            if len(self._format_context_prompt(states)) <= budget:
                break

    def _format_context_prompt(self, states: List[dict]) -> str:
        if not states:
            return ""
        lines = ["\n[ACTIVE TASK CONTEXT & SHORT-TERM MEMORY]"]
        for s in states:
            lines.append(f"- Key: {s['key']}")
            lines.append(f"  Value: {json.dumps(s['value'])}")
            lines.append(f"  Last Updated: {s['updated_at']}")
        lines.append("")
        return "\n".join(lines)

    def get_system_prompt_context(self) -> str:
        self._expire_stale_states()
        states = self.list_states()
        return self._format_context_prompt(states)

    def dump_diagnostics(self) -> dict:
        """Dumps details on key counts, priorities, and TTL states."""
        self._expire_stale_states()
        states = self.list_states()
        now = datetime.now(timezone.utc)
        
        details = []
        for s in states:
            priority = self._get_priority(s["key"])
            priority_label = "High" if priority == 2 else "Normal"
            
            updated_at = datetime.fromisoformat(s["updated_at"].replace("Z", "+00:00"))
            age = (now - updated_at).total_seconds()
            ttl_rem = max(0, self.ttls.get(priority, 1800) - age)
            
            details.append({
                "key": s["key"],
                "priority": priority_label,
                "value_length_chars": len(json.dumps(s["value"])),
                "age_seconds": round(age, 1),
                "ttl_remaining_seconds": round(ttl_rem, 1)
            })
            
        formatted_prompt = self._format_context_prompt(states)
        return {
            "active_keys_count": len(states),
            "formatted_size_chars": len(formatted_prompt),
            "budget_limit_chars": 1500,
            "details": details
        }
