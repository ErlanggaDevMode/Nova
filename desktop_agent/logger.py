import json
import os
from datetime import datetime, timezone
from pathlib import Path

class NovaLogger:
    def __init__(self, log_path: Path | str | None = None):
        if log_path is None:
            # Default to nova.log in the workspace root (parent of desktop_agent)
            log_path = Path(__file__).parent.parent / "nova.log"
        self.log_path = Path(log_path)

    def _append_log(self, entry: dict) -> None:
        # Ensure parent directories exist
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def log_command(self, raw_text: str, source_device_id: str = "desktop") -> str:
        """Log an incoming user command and return a generated command_id."""
        command_id = os.urandom(8).hex()
        entry = {
            "type": "command",
            "command_id": command_id,
            "raw_text": raw_text,
            "source_device_id": source_device_id,
        }
        self._append_log(entry)
        return command_id

    def log_action(self, action_id: str, command_id: str | None, action_type: str, category: str, params: dict, permission_decision: dict, executed: bool, result: dict | None = None) -> None:
        """Log an action request, its permission decision, execution status, and result."""
        entry = {
            "type": "action",
            "action_id": action_id,
            "command_id": command_id,
            "action_type": action_type,
            "category": category,
            "params": params,
            "permission_decision": permission_decision,
            "executed": executed,
            "result": result,
        }
        self._append_log(entry)
